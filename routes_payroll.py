"""
routes_payroll.py — Payroll tool (Tools → Payroll): reconcile paychecks so
the books show GROSS income before taxes, even though bank imports only
show NET direct deposits.

Two entry paths share one data model (payroll_reconciliations +
payroll_reconciliation_trxs):

  PATH 1 — Gusto CSV import. Upload a Gusto "Payroll Journal Report" CSV;
  each pay period parses into a draft reconciliation. Employee-side fields
  only (gross, taxes withheld, pre-tax deductions, net) — a personal user
  doesn't book employer-side payroll costs.

  PATH 2 — Manual paystub true-up. Type one paycheck's numbers off a
  paystub, or YTD totals as of a date for an annual true-up. Validated
  client- and server-side: gross − taxes − pre-tax − post-tax − other = net.

On execute, a reconciliation books "gross-up" rows: if a matching imported
net deposit exists, that deposit is split into children (+gross income,
−taxes withheld, −pre-tax deductions, −post-tax Roth/garnishment) that sum
back to net; otherwise
(YTD mode, or no deposit imported) the same rows book standalone.
Everything is undoable — the audit trail in payroll_reconciliation_trxs
records exactly what was created or changed.

No blueprints: register(app, helpers) binds every view under its endpoint
name, same convention as the other routes_* modules.
"""
import os
import csv as _csv
import io as _io
import json as _json
import hashlib as _hashlib
from datetime import datetime

from flask import request, redirect, url_for, render_template, jsonify, flash
from werkzeug.utils import secure_filename

import config
from config import OWNER
from db import get_db


# ─── Booking categories (edit to taste) ──────────────────────────────────────
# Every booked row is trx_type='income': positive = income, negative =
# contra-income (taxes withheld / pre-tax deductions), so gross income and
# take-home are both visible in the income views. These L1/L2 pairs come
# from the starter tree in categories.py — change them here if you've
# renamed categories in your own tree.
CAT_GROSS           = ("Salary & Wages", "Primary Job")
CAT_TAX_FEDERAL     = ("Taxes", "Federal Income Tax")
CAT_TAX_STATE       = ("Taxes", "State Income Tax")
CAT_TAX_SS          = ("Taxes", "Social Security")
CAT_TAX_MEDICARE    = ("Taxes", "Medicare")
CAT_TAX_FICA        = ("Taxes", "FICA (SS + Medicare)")   # combined, when a
                                                          # paystub shows one FICA line
CAT_TAX_OTHER       = ("Taxes", "Other Payroll Tax")      # PFML, SDI, local, …
CAT_PRETAX_OTHER    = ("Pre-Tax Deductions", "Health & Other Benefits")
# Garnishment / other POST-tax deduction: money you earned that was taken
# after tax — contra-income with its own clear label (NOT a tax, NOT the
# generic plug). Seeded in categories.py alongside Pre-Tax Deductions.
CAT_POSTTAX_GARNISH = ("Post-Tax Deductions", "Garnishments & Other")
CAT_OTHER_PLUG      = ("Miscellaneous", "Other")          # the "other" plug line

# RETIREMENT (pre-tax 401k AND post-tax Roth alike) is NOT contra-income —
# it's money moved into the user's own retirement account, so it books as
# trx_type='transfer'. When the
# reconciliation targets one of your Investments accounts, the transfer's
# L1 is that account's group and its L2 is the account NAME — exactly the
# shape the Investments Sync engine pulls as a contribution event (see
# routes_investments._sync_investment_events). With no target account the
# transfer books under this generic fallback category instead (Sync skips
# it; you can retarget later — see the handbook's payroll section).
CAT_RETIRE_L1_FALLBACK = "Retirement"
CAT_RETIRE_L2_FALLBACK = "Retirement Plan"

# Dropdown sentinel: explicitly skip Investments targeting for this rec.
RETIRE_SKIP = "none"

# Numeric columns shared by the draft form, the DB row, and the snapshot.
NUM_FIELDS = (
    "gross_earnings",
    "pretax_retirement", "pretax_other",
    "tax_federal", "tax_state", "tax_ss", "tax_medicare", "tax_other",
    "posttax_roth", "posttax_garnish",
    "other_plug",
    "net_pay",
)

TIE_TOL = 0.02   # cents tolerance for "the math ties"


def _rec_get(rec, key, default=0.0):
    """Numeric field from a sqlite3.Row or dict, tolerating a missing key
    (e.g. a row loaded before the post-tax columns were migrated in)."""
    try:
        v = rec[key]
    except (KeyError, IndexError):
        return default
    return v if v is not None else default


# ─── Gusto CSV parsing ───────────────────────────────────────────────────────

# Column classification for the Gusto Payroll Journal Report. Employee-side
# only — every "(Employer)" / "(Company Contribution)" column is ignored.
_RETIREMENT_HINTS = ("401(k)", "401k", "403(b)", "403b", "457", "ira",
                     "retirement")


def _classify_gusto_column(col):
    """Map one CSV header to a NUM_FIELDS bucket (or None to ignore)."""
    c = col.strip()
    lc = c.lower()
    if c == "Gross Earnings":
        return "gross_earnings"
    if c == "Net Pay":
        return "net_pay"
    if c.endswith("(Employee Deduction)"):
        # Order matters: "Roth 401(k)" contains a retirement hint, so Roth
        # (post-tax) must be checked BEFORE the pre-tax retirement bucket.
        if "roth" in lc:
            return "posttax_roth"
        if "garnish" in lc or "child support" in lc:
            return "posttax_garnish"
        if any(h in lc for h in _RETIREMENT_HINTS):
            return "pretax_retirement"
        return "pretax_other"
    # Garnishment columns sometimes appear without the "(Employee Deduction)"
    # suffix (e.g. "Garnishment", "Child Support Garnishment") — still
    # employee-side money taken post-tax. Employer-side columns never are.
    if ("garnish" in lc and "(employer" not in lc
            and not c.endswith("(Company Contribution)")):
        return "posttax_garnish"
    if c.endswith("(Employee)"):
        if c == "Federal Income Tax (Employee)":
            return "tax_federal"
        if c == "Social Security (Employee)":
            return "tax_ss"
        if c in ("Medicare (Employee)", "Additional Medicare (Employee)"):
            return "tax_medicare"
        if "withholding" in lc:
            return "tax_state"          # e.g. "CA Withholding Tax (Employee)"
        return "tax_other"              # PFML / SDI / local etc.
    return None                          # names, dates, employer-side, totals


def parse_gusto_csv(text):
    """Parse a Gusto Payroll Journal Report CSV into employee-side period
    dicts. A file may contain multiple pay periods; each becomes one dict
    with the NUM_FIELDS values plus period dates. Layout markers per period:

        "Payroll period", " 04/01/2026 - 04/15/2026"
        "Pay day",        " 04/15/2026"
        <header row starting with "Last Name">
        <employee data row(s)>
        "Payroll Totals", ...
    """
    reader = _csv.reader(_io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]

    def to_iso(s):
        s = s.strip()
        if not s:
            return None
        try:
            return datetime.strptime(s, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            return None

    periods = []
    i = 0
    while i < len(rows):
        if rows[i] and rows[i][0].strip() == "Payroll period":
            period_str = rows[i][1].strip() if len(rows[i]) > 1 else ""
            # Pay day on a following row
            pay_day_str = ""
            j = i + 1
            while j < len(rows):
                if rows[j] and rows[j][0].strip() == "Pay day":
                    pay_day_str = rows[j][1].strip() if len(rows[j]) > 1 else ""
                    break
                j += 1
            # Header row (starts with "Last Name")
            hdr_idx = None
            for k in range(j + 1, len(rows)):
                if rows[k] and rows[k][0].strip() == "Last Name":
                    hdr_idx = k
                    break
            if hdr_idx is None:
                i += 1
                continue
            header = [c.strip() for c in rows[hdr_idx]]
            # First employee data row (NOT the "Payroll Totals" row)
            data_row = None
            for k in range(hdr_idx + 1, len(rows)):
                if not rows[k]:
                    continue
                first = rows[k][0].strip()
                if first == "Payroll Totals" or first == "":
                    if data_row is not None:
                        break
                    continue
                data_row = rows[k]
                break
            if data_row is None:
                i = hdr_idx + 1
                continue

            row_dict = dict(zip(header, [c.strip() for c in data_row]))

            period = {n: 0.0 for n in NUM_FIELDS}
            for col, val in row_dict.items():
                bucket = _classify_gusto_column(col)
                if bucket is None:
                    continue
                try:
                    period[bucket] += float(val) if val else 0.0
                except ValueError:
                    pass
            for n in NUM_FIELDS:
                period[n] = round(period[n], 2)

            period_start_iso = period_end_iso = None
            if " - " in period_str:
                a, b = period_str.split(" - ", 1)
                period_start_iso = to_iso(a)
                period_end_iso = to_iso(b)
            period["pay_period_start"] = period_start_iso
            period["pay_period_end"] = period_end_iso
            period["pay_date"] = to_iso(pay_day_str)
            period["employee_name"] = (
                f"{row_dict.get('First Name', '').strip()} "
                f"{row_dict.get('Last Name', '').strip()}").strip()

            calc_net = round(
                period["gross_earnings"]
                - period["pretax_retirement"] - period["pretax_other"]
                - period["tax_federal"] - period["tax_state"]
                - period["tax_ss"] - period["tax_medicare"]
                - period["tax_other"]
                - period["posttax_roth"] - period["posttax_garnish"]
                - period["other_plug"], 2)
            period["calc_net_pay"] = calc_net
            period["net_pay_matches"] = abs(calc_net - period["net_pay"]) <= TIE_TOL

            periods.append(period)
            i = max(hdr_idx, j) + 1
        else:
            i += 1

    return periods


# ─── Matching + planning (pure reads) ────────────────────────────────────────

def find_payroll_matches(db, rec):
    """Find candidate net-pay deposits already imported from the bank:
    approved income transactions with amount == rec.net_pay (to the penny),
    not already claimed by another reconciliation, ranked by date proximity
    to the pay date. Returns {"net_deposit": {status, trx, candidates,
    expected, label, search}}.

    YTD entries return status 'not_applicable' — a YTD true-up spans many
    deposits, so it books standalone rows instead of splitting one.
    """
    net_amt = round(rec["net_pay"], 2)
    label = "Net pay deposit (imported from bank)"

    if rec["entry_mode"] == "ytd":
        return {"net_deposit": {
            "status": "not_applicable", "trx": None, "candidates": [],
            "expected": net_amt, "label": label,
            "search": "skipped — YTD entries book standalone rows "
                      "(they span many deposits)"}}

    other_claimed = {r["trx_id"] for r in db.execute(
        "SELECT trx_id FROM payroll_reconciliation_trxs WHERE reconciliation_id != ?",
        (rec["id"],))}

    rows = [r for r in db.execute("""
        SELECT t.*, a.name AS account_name
          FROM transactions t JOIN accounts a ON a.id = t.account_id
         WHERE t.owner = ? AND t.status = 'active' AND t.trx_type = 'income'
           AND t.parent_id IS NULL
           AND ABS(t.amount - ?) < 0.02
         ORDER BY ABS(julianday(t.trx_date) - julianday(?)) ASC, t.id ASC
         LIMIT 5
    """, (OWNER, net_amt, rec["pay_date"])).fetchall()
        if r["id"] not in other_claimed]

    search = (f"income transaction, amount=${net_amt:,.2f}, "
              f"closest to {rec['pay_date']}")
    if not rows:
        return {"net_deposit": {"status": "missing", "trx": None,
                                "candidates": [], "expected": net_amt,
                                "label": label, "search": search}}
    return {"net_deposit": {"status": "matched", "trx": rows[0],
                            "candidates": rows[1:], "expected": net_amt,
                            "label": label, "search": search}}


def _investment_accounts(db):
    """Active investment accounts (name + group) for the target dropdown."""
    return db.execute("""
        SELECT id, name, inv_group FROM accounts
         WHERE type='investment' AND active=1
         ORDER BY name""").fetchall()


def resolve_retirement_target(db, rec):
    """Decide where the pre-tax retirement transfer points. Returns
    {"l1", "l2", "account", "synced"}: account is the matched investment
    account name (or None), synced=True means the row will satisfy the
    Investments Sync criteria (L1 = the account's group, L2 = its name).

    Resolution: the rec's saved retirement_account if it names a real
    investment account; 'none' = explicit skip; otherwise auto-pick — the
    single Retirement-group investment account, else the single investment
    account overall; else the generic fallback category (Sync skips it).
    """
    saved = None
    if "retirement_account" in rec.keys():
        saved = (rec["retirement_account"] or "").strip() or None
    fallback = {"l1": CAT_RETIRE_L1_FALLBACK, "l2": CAT_RETIRE_L2_FALLBACK,
                "account": None, "synced": False}
    if saved == RETIRE_SKIP:
        return fallback
    accounts = _investment_accounts(db)
    by_name = {a["name"]: a for a in accounts}
    pick = None
    if saved and saved in by_name:
        pick = by_name[saved]
    else:
        retirement = [a for a in accounts
                      if (a["inv_group"] or "") == "Retirement"]
        if len(retirement) == 1:
            pick = retirement[0]
        elif len(accounts) == 1:
            pick = accounts[0]
    if pick is None:
        return fallback
    return {"l1": pick["inv_group"] or CAT_RETIRE_L1_FALLBACK,
            "l2": pick["name"], "account": pick["name"], "synced": True}


def plan_payroll_splits(rec, retirement_target=None):
    """Compute the gross-up rows this reconciliation WOULD book. Pure
    function, no DB writes. Returns {"children": [...], "children_sum": x,
    "delta": children_sum − net_pay, "ties": bool}.

    Each child: {type, l1, l2, amount (signed), sign, note}. Income rows:
    positive = income, negative = contra-income (taxes withheld / health
    pre-tax / garnishment & other post-tax). RETIREMENT amounts — pre-tax
    401k AND post-tax Roth alike — are negative trx_type='transfer' rows
    (money moved to your own retirement account, not income lost); both
    share the same retirement_target (from resolve_retirement_target(db,
    rec), defaulting to the generic fallback category), so when both exist
    they book as two transfer rows into the same account. The children sum
    to net (gross − taxes − pre-tax − post-tax − plug = net), which is
    exactly what the bank deposit shows — so splitting the deposit (or
    booking standalone) never changes cash totals, only reveals gross.
    """
    children = [{"type": "income", "l1": CAT_GROSS[0], "l2": CAT_GROSS[1],
                 "amount": round(rec["gross_earnings"], 2), "sign": "+",
                 "note": "gross wages"}]

    deductions = [
        (CAT_TAX_FEDERAL, rec["tax_federal"], "federal income tax withheld"),
        (CAT_TAX_STATE,   rec["tax_state"],   "state income tax withheld"),
    ]
    # FICA: separate SS + Medicare rows when both are known (Gusto CSV);
    # a single combined-FICA row when only one number was entered (paystub).
    if rec["tax_medicare"] and rec["tax_medicare"] > 0:
        deductions.append((CAT_TAX_SS, rec["tax_ss"],
                           "Social Security withheld"))
        deductions.append((CAT_TAX_MEDICARE, rec["tax_medicare"],
                           "Medicare withheld"))
    else:
        deductions.append((CAT_TAX_FICA, rec["tax_ss"],
                           "FICA withheld (Social Security + Medicare)"))
    deductions += [
        (CAT_TAX_OTHER,    rec["tax_other"],
         "other payroll taxes withheld (PFML / SDI / local)"),
        (CAT_PRETAX_OTHER, rec["pretax_other"],
         "pre-tax health / other benefits"),
        (CAT_POSTTAX_GARNISH, _rec_get(rec, "posttax_garnish"),
         "garnishment / other post-tax deduction"),
    ]
    for (l1, l2), amt, note in deductions:
        if amt and abs(amt) >= 0.005:
            children.append({"type": "income", "l1": l1, "l2": l2,
                             "amount": round(-amt, 2), "sign": "−",
                             "note": f"{note} (contra-income)"})

    # Retirement → transfer rows (negative = out of checking, into your
    # retirement account): pre-tax 401k and post-tax Roth each book their
    # own transfer, sharing the same target. When the target names an
    # investment account, the Investments Sync engine turns each row into a
    # contribution event.
    t = retirement_target or {"l1": CAT_RETIRE_L1_FALLBACK,
                              "l2": CAT_RETIRE_L2_FALLBACK,
                              "account": None, "synced": False}
    for amt, what in ((rec["pretax_retirement"] or 0.0,
                       "pre-tax retirement (401k etc.)"),
                      (_rec_get(rec, "posttax_roth"),
                       "Roth / post-tax retirement")):
        if abs(amt) >= 0.005:
            note = f"{what} → " + (t["account"] or "retirement plan")
            if t["synced"]:
                note += " (syncs to Investments as a contribution)"
            children.append({"type": "transfer", "l1": t["l1"], "l2": t["l2"],
                             "amount": round(-amt, 2), "sign": "−",
                             "note": note})

    plug = rec["other_plug"] or 0.0
    if abs(plug) >= 0.005:
        children.append({"type": "income",
                         "l1": CAT_OTHER_PLUG[0], "l2": CAT_OTHER_PLUG[1],
                         "amount": round(-plug, 2),
                         "sign": "−" if plug > 0 else "+",
                         "note": "other paycheck adjustment (plug)"})

    children_sum = round(sum(c["amount"] for c in children), 2)
    delta = round(children_sum - rec["net_pay"], 2)
    return {"children": children, "children_sum": children_sum,
            "delta": delta, "ties": abs(delta) <= TIE_TOL}


# ─── Execute / undo (DB writes; caller owns commit/rollback) ─────────────────

def _first_deposit_account(db):
    """Account to book standalone rows on: first active checking account,
    else the first account at all."""
    row = db.execute("""SELECT id FROM accounts
                         WHERE type='checking' AND active=1
                         ORDER BY id LIMIT 1""").fetchone()
    if not row:
        row = db.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("No accounts exist — create an account first")
    return row["id"]


def execute_payroll_reconciliation(db, rec_id):
    """Apply a draft reconciliation: split the matched net deposit into
    gross-up children (or book them standalone when there's no deposit to
    split), write the audit trail, flip status to 'reconciled'. Raises on
    any inconsistency; the caller commits/rolls back.
    """
    rec = db.execute("SELECT * FROM payroll_reconciliations WHERE id=?",
                     (rec_id,)).fetchone()
    if not rec:
        raise ValueError(f"Reconciliation {rec_id} not found")
    if rec["status"] not in ("draft", "undone"):
        raise ValueError(f"Reconciliation {rec_id} is {rec['status']!r}; "
                         "only 'draft' or 'undone' can be executed")

    retirement_target = resolve_retirement_target(db, rec)
    plan = plan_payroll_splits(rec, retirement_target)
    if not plan["ties"]:
        raise ValueError(
            f"Doesn't tie: gross − taxes − pre-tax − post-tax − other = "
            f"${plan['children_sum']:,.2f} but net pay is "
            f"${rec['net_pay']:,.2f} (off by ${abs(plan['delta']):,.2f}). "
            "Adjust the numbers or use the Other adjustment field.")

    matches = find_payroll_matches(db, rec)
    m = matches["net_deposit"]
    parent = m["trx"] if m["status"] == "matched" else None

    audit_rows = []
    new_trx_ids = []
    kids = plan["children"]

    if parent is not None:
        # ── Split the imported deposit into gross-up children ────────────
        pre = {"is_split": parent["is_split"], "status": parent["status"],
               "l1_category": parent["l1_category"],
               "l2_category": parent["l2_category"]}
        audit_rows.append({"trx_id": parent["id"], "role": "source",
                           "pre_state_json": _json.dumps(pre)})
        for i, c in enumerate(kids):
            raw = f"{parent['raw_description']} [payroll-recon {i+1}/{len(kids)}]"
            dk = _hashlib.md5(
                f"{rec['pay_date']}|{raw.upper()}|{c['amount']:.2f}".encode()
            ).hexdigest()
            note = f"Payroll reconciliation #{rec_id} — {c['note']}"
            if parent["trx_date"] != rec["pay_date"]:
                note += (f" — date normalized from bank date "
                         f"{parent['trx_date']} to pay date {rec['pay_date']}")
            cur = db.execute("""
                INSERT INTO transactions
                  (account_id, parent_id, is_split,
                   trx_date, post_date, statement_date, payment_date,
                   raw_description, vendor, amount, trx_type, owner,
                   l1_category, l2_category, note, receipt_path, dedup_key,
                   no_receipt_needed, status)
                VALUES (?,?,1, ?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, 1,'active')
            """, (
                parent["account_id"], parent["id"],
                rec["pay_date"], rec["pay_date"], rec["pay_date"], rec["pay_date"],
                raw, parent["vendor"], c["amount"], c["type"], OWNER,
                c["l1"], c["l2"], note, parent["receipt_path"], dk,
            ))
            new_trx_ids.append(cur.lastrowid)
            audit_rows.append({"trx_id": cur.lastrowid, "role": "derived",
                               "pre_state_json": None})
        db.execute("UPDATE transactions SET is_split=1, status='deleted' WHERE id=?",
                   (parent["id"],))
    else:
        # ── No deposit to split (YTD, or not imported) → standalone rows ─
        account_id = _first_deposit_account(db)
        mode_label = "YTD true-up" if rec["entry_mode"] == "ytd" else "paycheck true-up"
        for i, c in enumerate(kids):
            raw = (f"PAYROLL {mode_label.upper()} #{rec_id} "
                   f"({rec['pay_period_end']}) [{i+1}/{len(kids)}]")
            dk = _hashlib.md5(
                f"{rec['pay_date']}|{raw.upper()}|{c['amount']:.2f}|payroll-{rec_id}".encode()
            ).hexdigest()
            cur = db.execute("""
                INSERT INTO transactions
                  (account_id, trx_date, post_date, statement_date, payment_date,
                   raw_description, vendor, amount, trx_type, owner,
                   l1_category, l2_category, note, dedup_key,
                   no_receipt_needed, status)
                VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, 1,'active')
            """, (
                account_id,
                rec["pay_date"], rec["pay_date"], rec["pay_date"], rec["pay_date"],
                raw, "Payroll", c["amount"], c["type"], OWNER,
                c["l1"], c["l2"],
                f"Payroll reconciliation #{rec_id} ({mode_label}) — {c['note']}",
                dk,
            ))
            new_trx_ids.append(cur.lastrowid)
            audit_rows.append({"trx_id": cur.lastrowid, "role": "standalone",
                               "pre_state_json": None})

    for a in audit_rows:
        db.execute("""
            INSERT OR IGNORE INTO payroll_reconciliation_trxs
              (reconciliation_id, trx_id, role, pre_state_json)
            VALUES (?, ?, ?, ?)
        """, (rec_id, a["trx_id"], a["role"], a["pre_state_json"]))

    db.execute("""
        UPDATE payroll_reconciliations
           SET status='reconciled', reconciled_at=datetime('now'), undone_at=NULL
         WHERE id=?
    """, (rec_id,))

    # ── Investments wiring: ONE path, the existing Sync engine ──────────────
    # The retirement row above is a checking-side transfer whose L2 names an
    # investment account (when targeted) — exactly what the Sync engine pulls
    # as a contribution event. Sync is idempotent (linked_trx_id guard), so
    # this can never double-create; and because Sync is the only path, no
    # direct event creation happens here. Non-blocking, same as every other
    # auto-sync call site (routes_review approve).
    if retirement_target["synced"]:
        try:
            import routes_investments as _inv
            _inv.auto_sync_after_change(db)
        except Exception:
            pass

    return {"ok": True, "rec_id": rec_id, "new_trx_ids": new_trx_ids,
            "mode": "split" if parent is not None else "standalone",
            "retirement_target": retirement_target,
            "audit_count": len(audit_rows)}


def undo_payroll_reconciliation(db, rec_id):
    """Reverse a reconciled payroll: delete derived + standalone rows,
    restore the split parent from its snapshot, drop the audit rows, flip
    status to 'undone'."""
    rec = db.execute("SELECT * FROM payroll_reconciliations WHERE id=?",
                     (rec_id,)).fetchone()
    if not rec:
        raise ValueError(f"Reconciliation {rec_id} not found")
    if rec["status"] != "reconciled":
        raise ValueError(f"Reconciliation {rec_id} is {rec['status']!r}, "
                         "not 'reconciled'; refusing to undo")

    audit = db.execute("""
        SELECT id, trx_id, role, pre_state_json
          FROM payroll_reconciliation_trxs
         WHERE reconciliation_id=? ORDER BY id
    """, (rec_id,)).fetchall()

    try:
        import routes_investments as _inv
    except Exception:
        _inv = None

    deleted_count = restored_count = 0
    for a in audit:
        if a["role"] in ("derived", "standalone"):
            # If the retirement transfer synced a contribution into
            # Investments, remove that event first (auto_unsync_trx only
            # reverses a PRISTINE contribution — same rule as deleting any
            # linked transfer). Anything more entangled keeps its event;
            # the link is detached so the trx delete succeeds and the value
            # history stays intact (remove it manually from the ledger).
            if _inv is not None:
                _inv.auto_unsync_trx(db, a["trx_id"])
            db.execute("UPDATE investment_events SET linked_trx_id=NULL "
                       "WHERE linked_trx_id=?", (a["trx_id"],))
            db.execute("DELETE FROM transactions WHERE id=?", (a["trx_id"],))
            deleted_count += 1
    for a in audit:
        if a["role"] == "source" and a["pre_state_json"]:
            pre = _json.loads(a["pre_state_json"])
            db.execute("""
                UPDATE transactions
                   SET is_split=?, status=?, l1_category=?, l2_category=?,
                       updated_at=datetime('now')
                 WHERE id=?
            """, (pre.get("is_split", 0), pre.get("status", "active"),
                  pre.get("l1_category"), pre.get("l2_category"),
                  a["trx_id"]))
            restored_count += 1

    db.execute("DELETE FROM payroll_reconciliation_trxs WHERE reconciliation_id=?",
               (rec_id,))
    db.execute("""
        UPDATE payroll_reconciliations
           SET status='undone', undone_at=datetime('now'), reconciled_at=NULL
         WHERE id=?
    """, (rec_id,))

    return {"ok": True, "rec_id": rec_id,
            "deleted_count": deleted_count, "restored_count": restored_count}


# ─── Views ───────────────────────────────────────────────────────────────────

def tools_payroll():
    """Landing page: explainer + upload form + manual entry link +
    reconciliations list + import history."""
    db = get_db()
    status = request.args.get("status", "draft")
    if status == "draft":
        where = "WHERE status IN ('draft', 'undone')"
    elif status == "reconciled":
        where = "WHERE status = 'reconciled'"
    else:
        where = ""

    rows = db.execute(f"""
        SELECT id, entry_mode, pay_period_start, pay_period_end, pay_date,
               gross_earnings, net_pay, status, created_at, reconciled_at
          FROM payroll_reconciliations
         {where}
         ORDER BY pay_period_end DESC, id DESC
    """).fetchall()

    counts = {"draft": 0, "reconciled": 0, "undone": 0}
    for r in db.execute(
            "SELECT status, COUNT(*) c FROM payroll_reconciliations GROUP BY status"):
        counts[r["status"]] = r["c"]

    imports = db.execute("""
        SELECT source_csv_filename    AS filename,
               MIN(created_at)        AS uploaded_at,
               COUNT(*)               AS period_count,
               MIN(pay_period_start)  AS period_start,
               MAX(pay_period_end)    AS period_end
          FROM payroll_reconciliations
         WHERE source_csv_filename IS NOT NULL
         GROUP BY source_csv_filename
         ORDER BY MIN(created_at) DESC
    """).fetchall()

    return render_template("payroll/home.html",
                           reconciliations=rows, status=status,
                           counts=counts, imports=imports)


def tools_payroll_upload():
    """Parse an uploaded Gusto CSV; render the review form per period.
    The CSV auto-saves to imports/payroll/ for permanent import history."""
    f = request.files.get("csv")
    if not f or not f.filename:
        flash("No file uploaded.", "error")
        return redirect(url_for("tools_payroll"))

    safe_name = secure_filename(f.filename) or "gusto.csv"
    try:
        raw = f.stream.read()
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        flash(f"Couldn't read CSV: {e}", "error")
        return redirect(url_for("tools_payroll"))

    # config.IMPORTS_PAYROLL_DIR read dynamically (mirrors config.DB_PATH)
    # so tests can point uploads at a throwaway dir.
    os.makedirs(config.IMPORTS_PAYROLL_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    saved_filename = f"{ts}_{safe_name}"
    saved_path = os.path.join(config.IMPORTS_PAYROLL_DIR, saved_filename)
    try:
        with open(saved_path, "wb") as out:
            out.write(raw)
    except Exception as e:
        flash(f"Warning: couldn't save CSV to imports/payroll/ ({e}); "
              "reconciliations will work but no import history.", "error")
        saved_path = None

    periods = parse_gusto_csv(text)
    if not periods:
        flash("No payroll periods found in this file. Expected a Gusto "
              "Payroll Journal Report CSV.", "error")
        return redirect(url_for("tools_payroll"))

    db = get_db()
    for p in periods:
        existing = db.execute(
            """SELECT id, status FROM payroll_reconciliations
                WHERE pay_period_end=? AND entry_mode='gusto'""",
            (p["pay_period_end"],)).fetchone()
        p["existing_id"] = existing["id"] if existing else None
        p["existing_status"] = existing["status"] if existing else None

    return render_template("payroll/upload_review.html",
                           periods=periods, filename=safe_name,
                           source_csv_path=saved_path or "")


def tools_payroll_draft():
    """Save (or update) a draft reconciliation from any of the entry forms
    (Gusto review page, manual true-up form, or the view page). No
    transaction changes happen here — execute does that.

    Responds JSON for AJAX (Accept: application/json); otherwise flashes +
    redirects to the saved reconciliation's view page.
    """
    is_ajax = "application/json" in (request.headers.get("Accept") or "")

    def _resp(payload, status=200):
        if is_ajax:
            return jsonify(payload), status
        if not payload.get("ok") and payload.get("error"):
            flash(payload["error"], "error")
            return redirect(request.referrer or url_for("tools_payroll"))
        flash(payload.get("message", "Saved."), "success")
        return redirect(url_for("tools_payroll_view", rec_id=payload["id"]))

    db = get_db()
    data = request.form

    def f(name):
        v = (data.get(name) or "").strip()
        try:
            return float(v) if v else 0.0
        except ValueError:
            return 0.0

    entry_mode = (data.get("entry_mode") or "gusto").strip()
    if entry_mode not in ("gusto", "manual", "ytd"):
        return _resp({"ok": False, "error": f"Bad entry_mode {entry_mode!r}."}, 400)

    pay_date = (data.get("pay_date") or "").strip()
    pay_period_end = (data.get("pay_period_end") or "").strip() or pay_date
    if not pay_period_end or not pay_date:
        return _resp({"ok": False, "error": "Pay date is required."}, 400)
    pay_period_start = (data.get("pay_period_start") or "").strip()
    if not pay_period_start:
        # YTD entries default to Jan 1 of the as-of year; single paychecks
        # to the pay date itself.
        pay_period_start = (f"{pay_period_end[:4]}-01-01"
                            if entry_mode == "ytd" else pay_date)

    fields = {
        "entry_mode":          entry_mode,
        "pay_period_start":    pay_period_start,
        "pay_period_end":      pay_period_end,
        "pay_date":            pay_date,
        "source_csv_filename": (data.get("source_csv_filename") or "").strip() or None,
        "source_csv_path":     (data.get("source_csv_path") or "").strip() or None,
        "retirement_account":  (data.get("retirement_account") or "").strip() or None,
        "note":                (data.get("note") or "").strip() or None,
    }
    for n in NUM_FIELDS:
        fields[n] = f(n)

    if fields["gross_earnings"] <= 0:
        return _resp({"ok": False, "error": "Gross earnings must be > 0."}, 400)
    if fields["net_pay"] <= 0:
        return _resp({"ok": False, "error": "Net pay must be > 0."}, 400)

    # Source snapshot (Gusto path): hidden gusto_<field> inputs, captured on
    # first save only — powers the "matches source CSV" indicators.
    snapshot = {}
    for n in NUM_FIELDS:
        v = (data.get(f"gusto_{n}") or "").strip()
        if v:
            try:
                snapshot[n] = float(v)
            except ValueError:
                pass
    snapshot_json = _json.dumps(snapshot) if snapshot else None

    existing = db.execute(
        """SELECT id, status FROM payroll_reconciliations
            WHERE pay_period_end=? AND entry_mode=?""",
        (pay_period_end, entry_mode)).fetchone()

    if existing:
        if existing["status"] == "reconciled":
            return _resp({"ok": False, "error":
                          f"Period {pay_period_end} is already reconciled "
                          f"(id #{existing['id']}). Undo it first."}, 400)
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE payroll_reconciliations SET {sets} WHERE id=?",
                   list(fields.values()) + [existing["id"]])
        # Preserve an existing snapshot; backfill only if missing.
        cur_snap = db.execute(
            "SELECT source_values_json FROM payroll_reconciliations WHERE id=?",
            (existing["id"],)).fetchone()["source_values_json"]
        if not cur_snap and snapshot_json:
            db.execute("UPDATE payroll_reconciliations SET source_values_json=? WHERE id=?",
                       (snapshot_json, existing["id"]))
        saved_id, saved_action = existing["id"], "updated"
    else:
        cols = ", ".join(fields) + ", source_values_json"
        ph = ", ".join("?" * len(fields)) + ", ?"
        cur = db.execute(
            f"INSERT INTO payroll_reconciliations ({cols}, status) VALUES ({ph}, 'draft')",
            list(fields.values()) + [snapshot_json])
        saved_id, saved_action = cur.lastrowid, "created"

    db.commit()
    return _resp({"ok": True, "id": saved_id, "action": saved_action,
                  "status": "draft", "period_end": pay_period_end,
                  "message": f"Draft {saved_action} for {pay_period_end} "
                             f"(#{saved_id})."})


def tools_payroll_manual():
    """Manual paystub true-up form: one paycheck's numbers, or YTD totals
    as of a date. Saving creates a draft and lands on its view page."""
    db = get_db()
    return render_template("payroll/manual.html",
                           inv_accounts=_investment_accounts(db))


def tools_payroll_view(rec_id):
    """View / edit an existing reconciliation (draft or reconciled):
    the numbers, the net-deposit match, and the projected gross-up rows."""
    db = get_db()
    rec = db.execute("SELECT * FROM payroll_reconciliations WHERE id=?",
                     (rec_id,)).fetchone()
    if not rec:
        flash("Reconciliation not found.", "error")
        return redirect(url_for("tools_payroll"))

    source = {}
    if rec["source_values_json"]:
        try:
            source = _json.loads(rec["source_values_json"])
        except Exception:
            source = {}

    matches = find_payroll_matches(db, rec)
    retirement_target = resolve_retirement_target(db, rec)
    plan = plan_payroll_splits(rec, retirement_target)

    return render_template("payroll/view.html",
                           rec=rec, source=source,
                           matches=matches, plan=plan,
                           retirement_target=retirement_target,
                           inv_accounts=_investment_accounts(db))


def tools_payroll_execute(rec_id):
    db = get_db()
    try:
        result = execute_payroll_reconciliation(db, rec_id)
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify(result)


def tools_payroll_undo(rec_id):
    db = get_db()
    try:
        result = undo_payroll_reconciliation(db, rec_id)
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify(result)


def register(app, helpers):
    """Bind this module's routes; endpoint names == function names."""
    login_required = helpers["login_required"]
    global tools_payroll, tools_payroll_upload, tools_payroll_draft, \
        tools_payroll_manual, tools_payroll_view, tools_payroll_execute, \
        tools_payroll_undo
    tools_payroll = login_required(tools_payroll)
    app.route("/tools/payroll", methods=["GET"])(tools_payroll)
    tools_payroll_upload = login_required(tools_payroll_upload)
    app.route("/tools/payroll/upload", methods=["POST"])(tools_payroll_upload)
    tools_payroll_draft = login_required(tools_payroll_draft)
    app.route("/tools/payroll/draft", methods=["POST"])(tools_payroll_draft)
    tools_payroll_manual = login_required(tools_payroll_manual)
    app.route("/tools/payroll/manual", methods=["GET"])(tools_payroll_manual)
    tools_payroll_view = login_required(tools_payroll_view)
    app.route("/tools/payroll/<int:rec_id>", methods=["GET"])(tools_payroll_view)
    tools_payroll_execute = login_required(tools_payroll_execute)
    app.route("/tools/payroll/<int:rec_id>/execute", methods=["POST"])(tools_payroll_execute)
    tools_payroll_undo = login_required(tools_payroll_undo)
    app.route("/tools/payroll/<int:rec_id>/undo", methods=["POST"])(tools_payroll_undo)
