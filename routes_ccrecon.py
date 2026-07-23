"""
routes_ccrecon.py — CC Statement Reconciliation, the "close the card cycle"
wizard.

Ties one imported card PAYMENT to the exact set of card CHARGES it settled,
proves the math to the penny, updates each charge's payment_date to the
actual cash-leaving date AND its statement_date to the settled close, and
marks the charges settled (cc_settlements) so they can never be counted
twice.

Part of Personal Financial Tracker (PFT). No blueprints:
register(app, helpers) binds every view under its original function
name.

CARD MODEL IS DYNAMIC: every active credit-card account is reconcilable.
The registry is read from the accounts table (type='credit_card'), keyed by
account_num; a card's display name is its account name.

WHICH CARD DOES A PAYMENT PAY? A payment is a trx_type='transfer' with
l1_category='Credit Card Payment'; its L2 category names the card (the
credit-card account's NAME) — set by the checking importer default (when
there is exactly one card) or in the review queue / transaction editor.

Statement closes come from (any of): observed charge statement_dates,
seeded statement_balances rows, or the account's own statement close day
(accounts.stmt_close_day, set on Docs & Settings → Accounts → synthetic
monthly closes).

MATCHING MODEL:
  Charges bucket by STATEMENT date when the importer assigned one (from
  the account's close day), post-date fallback when absent. For a payment
  with cash date D settling close C (the latest close before D), the
  default candidate set = OPEN charges whose effective date
  (COALESCE(statement_date, post_date, trx_date)) is in (prev_close, C].
  Identity, to the penny:   Σ(set, semantic-signed) + carry == P
  Semantic signing: expense +, income (refund/credit) −.
  A payment SMALLER than the cycle's charges is not an error — the
  remainder is a carried balance (diagnosed, shown, never auto-settled).

Preview-first everywhere. NO silent writes.
"""
from datetime import date

from flask import request, jsonify, render_template

from config import OWNER
from db import get_db

LANDING_URL = "/tools/reconcile-card"
PAYMENT_L1 = "Credit Card Payment"


# ── Card registry (dynamic) ──────────────────────────────────────────────────

def get_cards(db):
    """Every active credit-card account is reconcilable. Keyed by
    account_num; the account NAME is how a payment's L2 names the card."""
    out = {}
    for r in db.execute(
            """SELECT account_num, name FROM accounts
                WHERE type='credit_card' AND active=1
                ORDER BY name"""):
        out[r["account_num"]] = {"name": r["name"],
                                 "bal_code": r["account_num"]}
    return out


def card_by_l2(db):
    """{card name → account_num} — resolves a payment's L2 to its card."""
    return {v["name"]: k for k, v in get_cards(db).items()}


def _r2(x):
    return round(float(x or 0), 2)


def semantic(row):
    """Statement-signed amount: expense +, income (refund) −."""
    a = float(row["amount"] or 0)
    return a if row["trx_type"] == "expense" else -a


def _synthetic_closes(db, acct):
    """Monthly closes from the account's own statement close day
    (accounts.stmt_close_day, set on Docs & Settings → Accounts), spanning
    the card's activity (so cards work even before statement_dates are
    populated). Close days 29–31 clamp to end-of-month where needed."""
    from billing import clamp_day
    row = db.execute(
        """SELECT stmt_close_day FROM accounts
            WHERE account_num=? AND type='credit_card'""", (acct,)).fetchone()
    if not row or not row["stmt_close_day"]:
        return set()
    span = db.execute(
        """SELECT MIN(COALESCE(t.post_date, t.trx_date)),
                  MAX(COALESCE(t.post_date, t.trx_date))
             FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE a.account_num=? AND t.status='active'""", (acct,)).fetchone()
    if not span or not span[0]:
        return set()
    lo = date.fromisoformat(span[0][:10])
    hi = max(date.fromisoformat(span[1][:10]), date.today())
    out, y, m = set(), lo.year, lo.month
    while (y, m) <= (hi.year, hi.month + (1 if hi.month < 12 else 0)) \
            and y <= hi.year + 1:
        out.add(clamp_day(y, m, row["stmt_close_day"]).isoformat())
        m += 1
        if m == 13:
            y, m = y + 1, 1
        if y > hi.year + 1:
            break
    return out


def _closes(db, acct):
    """All known statement closes: observed charge statement_dates + seeded
    statement_balances + synthetic (the account's stmt_close_day)."""
    s = {r[0] for r in db.execute(
        """SELECT DISTINCT statement_date FROM transactions t
             JOIN accounts a ON a.id = t.account_id
            WHERE a.account_num=? AND t.statement_date IS NOT NULL""",
        (acct,))}
    s |= {r[0] for r in db.execute(
        "SELECT statement_date FROM statement_balances WHERE coa_code=?",
        (acct,))}
    s |= _synthetic_closes(db, acct)
    return sorted(s)


def card_for_payment(db, pay):
    """The card a payment pays = its L2 category (or None if unset)."""
    return card_by_l2(db).get(pay["l2_category"] if pay else None)


def cycle_for_payment(db, pay_date, acct):
    """The close a payment settles = the latest close ≥20 days before its
    cash date. Autopay typically runs ~3-4 weeks after the close, and it
    can land AFTER the next close — 'latest close strictly before D' picks
    the wrong cycle there."""
    from datetime import datetime, timedelta
    closes = _closes(db, acct)
    try:
        cutoff = (datetime.strptime(pay_date[:10], "%Y-%m-%d")
                  - timedelta(days=20)).date().isoformat()
    except ValueError:
        cutoff = pay_date
    before = [c for c in closes if c <= cutoff]
    return before[-1] if before else None


def open_charges(db, lo, hi, include_ids=None, exclude_ids=None, acct=None):
    """OPEN (unsettled) charges on `acct` in the (lo, hi] window, plus
    explicit includes, minus explicit excludes. Bucketing basis: the
    charge's STATEMENT date when set (the importer assigns it from the
    account's close day), post-date fallback only when absent. Split
    children yes, split parents never."""
    include_ids = set(include_ids or [])
    exclude_ids = set(exclude_ids or [])
    rows = [dict(r) for r in db.execute(
        """SELECT t.id, t.trx_date, t.post_date, t.payment_date, t.vendor,
                  t.amount, t.trx_type, t.statement_date
             FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE a.account_num = ? AND t.status='active'
              AND t.trx_type IN ('expense','income')
              AND NOT (COALESCE(t.is_split,0)=1 AND t.parent_id IS NULL)
              AND t.id NOT IN (SELECT charge_id FROM cc_settlements)
            ORDER BY COALESCE(t.statement_date, t.post_date, t.trx_date)""",
        (acct,)).fetchall()]
    out = []
    for r in rows:
        eff = r["statement_date"] or r["post_date"] or r["trx_date"]
        in_window = (lo is None or eff > lo) and eff <= hi
        if r["id"] in exclude_ids:
            continue
        if in_window or r["id"] in include_ids:
            r["effective_date"] = eff
            r["semantic"] = _r2(semantic(r))
            r["included_manually"] = r["id"] in include_ids and not in_window
            out.append(r)
    return out


def carry_for_cycle(db, prev_close, acct):
    """Prior-close carry: if the previous close has a seeded statement
    balance but NO charges post on/before it (books start mid-history),
    that balance rides into this payment."""
    if not prev_close:
        return 0.0
    row = db.execute(
        "SELECT balance FROM statement_balances WHERE coa_code=? "
        "AND statement_date=?", (acct, prev_close)).fetchone()
    if not row:
        return 0.0
    n = db.execute(
        """SELECT COUNT(*) FROM transactions t
             JOIN accounts a ON a.id = t.account_id
            WHERE a.account_num=? AND t.status='active'
              AND t.trx_type IN ('expense','income')
              AND COALESCE(t.post_date, t.trx_date) <= ?
              AND NOT (COALESCE(t.is_split,0)=1 AND t.parent_id IS NULL)""",
        (acct, prev_close)).fetchone()[0]
    return _r2(row["balance"]) if n == 0 else 0.0


def preview(db, payment_id, include_ids=None, exclude_ids=None, cycle=None):
    """READ-ONLY preview: candidate set, proposed date moves, residual, and
    (when residual ≠ 0) the diagnosis block."""
    pay = db.execute(
        """SELECT * FROM transactions
            WHERE id=? AND trx_type='transfer'
              AND l1_category=? AND status='active'""",
        (payment_id, PAYMENT_L1)).fetchone()
    if not pay:
        return {"error": "not an active Credit Card Payment transfer"}
    cards = get_cards(db)
    acct = card_for_payment(db, pay)
    if not acct:
        names = " / ".join(c["name"] for c in cards.values()) or "your card"
        return {"error": "which card? set this payment's L2 category to the "
                         f"card name ({names}) first"}
    if db.execute("SELECT 1 FROM cc_recon_payments WHERE payment_id=?",
                  (payment_id,)).fetchone():
        return {"error": "payment already reconciled — unwind it first"}

    D = pay["trx_date"]
    P = _r2(abs(pay["amount"]))
    closes = _closes(db, acct)
    C = cycle or cycle_for_payment(db, D, acct)
    if not C:
        return {"error": "no statement close found before this payment — "
                         "set the card's statement close day on its account "
                         "(Docs & Settings → Accounts), or set statement "
                         "dates on its charges"}
    prev = ([c for c in closes if c < C] or [None])[-1]

    charges = open_charges(db, prev, C, include_ids, exclude_ids, acct)
    carry = carry_for_cycle(db, prev, acct)
    total = _r2(sum(c["semantic"] for c in charges))
    residual = _r2(P - total - carry)

    rows = []
    for c in charges:
        moves = (c["payment_date"] or "")[:7] != D[:7]
        stmt_moves = (c["statement_date"] or "") != C
        rows.append({
            "id": c["id"], "date": c["trx_date"], "post": c["post_date"],
            "vendor": c["vendor"], "amount": c["amount"],
            "trx_type": c["trx_type"], "semantic": c["semantic"],
            "statement_date": c["statement_date"],
            "statement_date_new": C,
            "stmt_move": (f"{c['statement_date'] or '—'} → {C}"
                          if stmt_moves else "no change"),
            "payment_date_now": c["payment_date"],
            "payment_date_new": D,
            "pl_move": (f"{(c['payment_date'] or c['trx_date'])[:7]} → {D[:7]}"
                        if moves else "no change"),
            "included_manually": c["included_manually"],
        })

    diagnosis = None
    if abs(residual) >= 0.005:
        diagnosis = diagnose(db, residual, prev, C,
                             {c["id"] for c in charges}, D, acct)

    return {
        "payment": {"id": pay["id"], "date": D, "amount": P},
        "card": {"acct": acct, "name": cards[acct]["name"]},
        "cycle": C, "prev_close": prev, "closes": closes,
        "carry": carry, "charges": rows, "total": total,
        "residual": residual, "ok": abs(residual) < 0.005,
        "diagnosis": diagnosis,
    }


def _date_plausible(eff, prev, C, D, days=2):
    """Date-awareness: don't suggest transactions from anywhere in history
    just because the amount matches. A genuinely missing charge is either
      (a) a BOUNDARY charge — within ~2 days of a close, i.e. it was
          ambiguous which statement it would land on; or
      (b) the ABSORBED-CREDIT pattern — posted after the close C but on/
          before the payment's cash date D, so the bank pulled it into the
          autopay amount."""
    from datetime import datetime, timedelta

    def _d(s):
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    try:
        e = _d(eff)
        w = timedelta(days=days)
        if prev and abs(e - _d(prev)) <= w:
            return True
        if abs(e - _d(C)) <= w:
            return True
        return D is not None and _d(C) < e <= _d(D)
    except Exception:
        return True   # unparseable date → don't hide it


def diagnose(db, residual, prev, C, in_set_ids, D=None, acct=None):
    """The 'help me catch errors' block for a non-zero residual."""
    out = {"singles": [], "pairs": [], "already_settled": [], "pending": [],
           "absorbed_credits": None, "carried_balance": None, "fallback": None}

    # CARRIED BALANCE: the payment is SMALLER than the cycle's charges.
    # That's not an error — a user who pays less than the statement balance
    # carries the remainder (and interest lands as a normal charge on a
    # later statement). Shown as a diagnosis, never auto-settled: to
    # confirm a partial payment, exclude the charges it didn't cover (they
    # stay open for the next payment).
    if residual < -0.005:
        out["carried_balance"] = {"amount": _r2(-residual)}

    # ABSORBED CREDITS: refunds post after the close, and the bank nets
    # every credit posted in (C, payday] out of the autopay amount. Credits
    # bucket by PAYMENT window, not statement window — offer them as one
    # group include.
    # NOTE: diagnosis works on POST dates (physical timing), not the
    # bucketing effective_date — a boundary charge's importer-assigned
    # statement date is exactly the thing that may be wrong.
    def _post_eff(c):
        return c["post_date"] or c["trx_date"]

    if D:
        creds = [c for c in open_charges(db, None, "9999-12-31", acct=acct)
                 if c["id"] not in in_set_ids and c["semantic"] < 0
                 and C < _post_eff(c) <= D]
        if creds:
            csum = _r2(sum(c["semantic"] for c in creds))
            out["absorbed_credits"] = {
                "sum": csum, "exact": abs(csum - residual) < 0.005,
                "items": [{"id": c["id"], "date": c["trx_date"],
                           "vendor": c["vendor"], "semantic": c["semantic"]}
                          for c in creds]}

    # Open charges OUTSIDE the set whose semantic == residual (the missed-
    # refund pattern: include it and the residual dies). Date-plausible only.
    all_open = open_charges(db, None, "9999-12-31", acct=acct)
    outside = [c for c in all_open if c["id"] not in in_set_ids
               and _date_plausible(_post_eff(c), prev, C, D)]
    for c in outside:
        if abs(c["semantic"] - residual) < 0.005:
            out["singles"].append({
                "id": c["id"], "date": c["trx_date"], "vendor": c["vendor"],
                "semantic": c["semantic"], "action": "include"})
    # In-set charges whose REMOVAL fixes it (excluding c changes total by
    # -c.semantic → residual + c.semantic = 0).
    inside = [c for c in all_open if c["id"] in in_set_ids]
    for c in inside:
        if abs(c["semantic"] + residual) < 0.005:
            out["singles"].append({
                "id": c["id"], "date": c["trx_date"], "vendor": c["vendor"],
                "semantic": c["semantic"], "action": "exclude"})
    # Pairs (≤2) among outside charges summing to residual.
    for i in range(len(outside)):
        for j in range(i + 1, len(outside)):
            if abs(outside[i]["semantic"] + outside[j]["semantic"] - residual) < 0.005:
                out["pairs"].append([
                    {"id": outside[i]["id"], "vendor": outside[i]["vendor"],
                     "semantic": outside[i]["semantic"]},
                    {"id": outside[j]["id"], "vendor": outside[j]["vendor"],
                     "semantic": outside[j]["semantic"]}])
                if len(out["pairs"]) >= 3:
                    break
        if len(out["pairs"]) >= 3:
            break
    # In-cycle charges already settled by ANOTHER payment (double-count risk).
    for r in db.execute(
            """SELECT t.id, t.trx_date, t.vendor, t.amount, t.trx_type,
                      s.payment_id
                 FROM transactions t
                 JOIN cc_settlements s ON s.charge_id = t.id
                 JOIN accounts a ON a.id = t.account_id
                WHERE a.account_num=? AND COALESCE(t.post_date, t.trx_date) > ?
                  AND COALESCE(t.post_date, t.trx_date) <= ?""",
            (acct, prev or "0000", C)).fetchall():
        out["already_settled"].append(dict(r))
    # Pending staging rows matching the residual.
    try:
        for r in db.execute(
                """SELECT id, raw_description, amount FROM staging
                    WHERE status='pending' AND ABS(ABS(amount) - ?) < 0.005""",
                (abs(residual),)).fetchall():
            out["pending"].append(dict(r))
    except Exception:
        pass
    if not (out["singles"] or out["pairs"] or out["already_settled"]
            or out["pending"] or out["carried_balance"]):
        out["fallback"] = (f"Books say this cycle sums to Σ+carry, the bank "
                           f"took a different amount (residual {residual:+.2f})"
                           f" — pull the statement PDF for the {C} close.")
    return out


def confirm(db, payment_id, include_ids=None, exclude_ids=None, cycle=None):
    """Apply a CLEAN preview in one transaction; returns the post-write
    verification summary. Refuses when residual ≠ 0."""
    pv = preview(db, payment_id, include_ids, exclude_ids, cycle)
    if pv.get("error"):
        return pv
    if not pv["ok"]:
        return {"error": f"residual {pv['residual']:+.2f} ≠ 0 — resolve the "
                         "diagnosis before confirming"}
    D = pv["payment"]["date"]
    C = pv["cycle"]
    for c in pv["charges"]:
        db.execute(
            """INSERT INTO cc_settlements
               (payment_id, charge_id, prev_payment_date, prev_statement_date)
               VALUES (?,?,?,?)""",
            (payment_id, c["id"], c["payment_date_now"], c["statement_date"]))
        # True-up BOTH dates: payment_date = the real cash-leaving date
        # (drives cash-basis P&L month); statement_date = the close this
        # charge actually settled on (fixes boundary charges the importer
        # guessed into the wrong cycle).
        db.execute(
            "UPDATE transactions SET payment_date=?, statement_date=? WHERE id=?",
            (D, C, c["id"]))
    db.execute(
        "INSERT INTO cc_recon_payments (payment_id, carry) VALUES (?,?)",
        (payment_id, pv["carry"]))
    db.commit()

    # Post-write verification.
    linked = db.execute(
        """SELECT t.id, t.payment_date, t.amount, t.trx_type
             FROM cc_settlements s JOIN transactions t ON t.id = s.charge_id
            WHERE s.payment_id=?""", (payment_id,)).fetchall()
    all_d = all(r["payment_date"] == D for r in linked)
    ssum = _r2(sum(semantic(r) for r in linked))
    return {"applied": True, "n_charges": len(linked),
            "all_payment_dates_set": all_d,
            "settled_sum": ssum, "carry": pv["carry"],
            "ties": abs(ssum + pv["carry"] - pv["payment"]["amount"]) < 0.005}


def unwind(db, payment_id):
    """Delete the payment's settlements, restore each charge's prior
    payment_date + statement_date, drop the recon marker."""
    rows = db.execute(
        "SELECT charge_id, prev_payment_date, prev_statement_date "
        "FROM cc_settlements WHERE payment_id=?", (payment_id,)).fetchall()
    if not rows and not db.execute(
            "SELECT 1 FROM cc_recon_payments WHERE payment_id=?",
            (payment_id,)).fetchone():
        return {"error": "payment is not reconciled"}
    for r in rows:
        db.execute(
            "UPDATE transactions SET payment_date=?, statement_date=? WHERE id=?",
            (r["prev_payment_date"], r["prev_statement_date"], r["charge_id"]))
    db.execute("DELETE FROM cc_settlements WHERE payment_id=?", (payment_id,))
    db.execute("DELETE FROM cc_recon_payments WHERE payment_id=?", (payment_id,))
    db.commit()
    return {"unwound": True, "n_charges": len(rows)}


def auto_unwind_for_trx(db, trx_id):
    """Trash-hook: if `trx_id` is a settled charge or a reconciled payment,
    unwind the affected settlement automatically. Returns a note for the
    caller's response, or None. Caller commits."""
    pay = db.execute("SELECT 1 FROM cc_recon_payments WHERE payment_id=?",
                     (trx_id,)).fetchone()
    if pay:
        unwind(db, trx_id)
        return f"payment {trx_id} was reconciled — settlement auto-unwound"
    ch = db.execute("SELECT payment_id FROM cc_settlements WHERE charge_id=?",
                    (trx_id,)).fetchone()
    if ch:
        unwind(db, ch["payment_id"])
        return (f"charge {trx_id} was settled by payment {ch['payment_id']} "
                f"— that settlement was auto-unwound (re-reconcile it)")
    return None


def charge_is_settled(db, trx_id):
    return bool(db.execute(
        "SELECT 1 FROM cc_settlements WHERE charge_id=?", (trx_id,)).fetchone())


# ─── Pages / APIs ────────────────────────────────────────────────────────────
def _all_payments(db):
    """CC payment transfers — the CHECKING-side rows only (card-side
    'Payment Thank You' rows stay skipped at import)."""
    return db.execute(
        """SELECT t.id, t.trx_date, t.amount, t.l2_category,
                  (SELECT COUNT(*) FROM cc_recon_payments rp
                    WHERE rp.payment_id = t.id) AS reconciled,
                  (SELECT COUNT(*) FROM cc_settlements s
                    WHERE s.payment_id = t.id) AS n_charges
             FROM transactions t
             JOIN accounts a ON a.id = t.account_id
            WHERE t.owner=? AND t.trx_type='transfer'
              AND t.l1_category=? AND t.status='active'
              AND a.type != 'credit_card'
            ORDER BY t.trx_date DESC""", (OWNER, PAYMENT_L1))


def _settled_detail(db, payment_id):
    """Charges settled by a reconciled payment + carry + tie math."""
    charges = [dict(r) for r in db.execute(
        """SELECT t.id, t.trx_date, t.post_date, t.vendor, t.amount,
                  t.trx_type, t.statement_date, t.payment_date
             FROM cc_settlements s JOIN transactions t ON t.id = s.charge_id
            WHERE s.payment_id=?
            ORDER BY COALESCE(t.post_date, t.trx_date)""",
        (payment_id,)).fetchall()]
    for c in charges:
        c["semantic"] = _r2(semantic(c))
    carry = db.execute(
        "SELECT carry FROM cc_recon_payments WHERE payment_id=?",
        (payment_id,)).fetchone()
    carry = _r2(carry["carry"]) if carry else 0.0
    cycle = max((c["statement_date"] or "" for c in charges), default=None)
    return charges, carry, cycle


def _open_charge_view(db, acct):
    """The 'accrued CC expenses' card: every OPEN (unsettled) charge on
    `acct`, bucketed by the statement close it will land on, with per-
    statement Expected tie (seeded statement balances)."""
    closes = _closes(db, acct)
    charges = open_charges(db, None, "9999-12-31", acct=acct)
    buckets = {}
    for c in charges:
        cl = next((x for x in closes if c["effective_date"] <= x), None)
        key = cl or (f"after {closes[-1]} (statement not closed yet)"
                     if closes else "no closes known")
        buckets.setdefault(key, []).append(c)
    groups = []
    for k in sorted(buckets):
        rows = buckets[k]
        sub = _r2(sum(r["semantic"] for r in rows))
        expected = None
        if not str(k).startswith(("after", "no closes")):
            e = db.execute(
                "SELECT balance FROM statement_balances WHERE coa_code=? "
                "AND statement_date=?", (acct, k)).fetchone()
            if e is not None:
                expected = _r2(e["balance"])
        groups.append({"close": k, "rows": rows, "subtotal": sub,
                       "expected": expected,
                       "delta": (_r2(sub - expected)
                                 if expected is not None else None)})
    open_sum = _r2(sum(g["subtotal"] for g in groups))
    return groups, {"open_sum": open_sum}


def _landing_ctx(db):
    cards_reg = get_cards(db)
    by_l2 = {v["name"]: k for k, v in cards_reg.items()}
    open_pays, done_pays = [], []
    for r in _all_payments(db):
        acct = by_l2.get(r["l2_category"])
        d = {**dict(r), "P": _r2(abs(r["amount"])),
             "card_name": cards_reg[acct]["name"] if acct else None,
             "cycle_guess": (cycle_for_payment(db, r["trx_date"], acct)
                             if acct else None)}
        if r["reconciled"]:
            charges, carry, cycle = _settled_detail(db, r["id"])
            d.update(cycle=cycle or d["cycle_guess"], carry=carry,
                     settled_sum=_r2(sum(c["semantic"] for c in charges)))
            done_pays.append(d)
        else:
            open_pays.append(d)
    cards = []
    for acct in cards_reg:
        groups, tie = _open_charge_view(db, acct)
        if groups or not cards:
            cards.append({"acct": acct, "name": cards_reg[acct]["name"],
                          "groups": groups, "tie": tie})
    return {"open_pays": open_pays, "done_pays": done_pays,
            "cards": cards, "detail_base": LANDING_URL,
            "multi_card": len(cards_reg) > 1}


def tools_reconcile_card():
    return render_template("tools/reconcile_card.html",
                           **_landing_ctx(get_db()))


def _detail_ctx(db, payment_id):
    pay = db.execute(
        """SELECT * FROM transactions
            WHERE id=? AND trx_type='transfer'
              AND l1_category=? AND status='active'""",
        (payment_id, PAYMENT_L1)).fetchone()
    if not pay:
        return None
    cards_reg = get_cards(db)
    acct = card_for_payment(db, pay)
    reconciled = bool(db.execute(
        "SELECT 1 FROM cc_recon_payments WHERE payment_id=?",
        (payment_id,)).fetchone())
    ctx = {"pay": {"id": pay["id"], "date": pay["trx_date"],
                   "P": _r2(abs(pay["amount"]))},
           "card_name": cards_reg[acct]["name"] if acct else None,
           "reconciled": reconciled,
           "landing_url": LANDING_URL}
    if reconciled:
        charges, carry, cycle = _settled_detail(db, payment_id)
        ssum = _r2(sum(c["semantic"] for c in charges))
        ctx.update(charges=charges, carry=carry, cycle=cycle,
                   settled_sum=ssum,
                   ties=abs(ssum + carry - ctx["pay"]["P"]) < 0.005)
    return ctx


def tools_reconcile_card_detail(payment_id):
    ctx = _detail_ctx(get_db(), payment_id)
    if not ctx:
        from flask import redirect
        return redirect(LANDING_URL)
    return render_template("tools/reconcile_card_detail.html", **ctx)


def api_ccrecon_preview():
    db = get_db()
    d = request.get_json(silent=True) or {}
    return jsonify(preview(db, d.get("payment_id"),
                           d.get("include_ids"), d.get("exclude_ids"),
                           d.get("cycle")))


def api_ccrecon_confirm():
    db = get_db()
    d = request.get_json(silent=True) or {}
    res = confirm(db, d.get("payment_id"),
                  d.get("include_ids"), d.get("exclude_ids"), d.get("cycle"))
    return jsonify(res), (400 if res.get("error") else 200)


def api_ccrecon_unwind():
    db = get_db()
    d = request.get_json(silent=True) or {}
    res = unwind(db, d.get("payment_id"))
    return jsonify(res), (400 if res.get("error") else 200)


def register(app, helpers):
    login_required = helpers["login_required"]
    global tools_reconcile_card, tools_reconcile_card_detail, \
        api_ccrecon_preview, api_ccrecon_confirm, api_ccrecon_unwind
    tools_reconcile_card = login_required(tools_reconcile_card)
    app.route("/tools/reconcile-card")(tools_reconcile_card)
    tools_reconcile_card_detail = login_required(tools_reconcile_card_detail)
    app.route("/tools/reconcile-card/<int:payment_id>")(
        tools_reconcile_card_detail)
    api_ccrecon_preview = login_required(api_ccrecon_preview)
    app.route("/api/ccrecon/preview", methods=["POST"])(api_ccrecon_preview)
    api_ccrecon_confirm = login_required(api_ccrecon_confirm)
    app.route("/api/ccrecon/confirm", methods=["POST"])(api_ccrecon_confirm)
    api_ccrecon_unwind = login_required(api_ccrecon_unwind)
    app.route("/api/ccrecon/unwind", methods=["POST"])(api_ccrecon_unwind)
