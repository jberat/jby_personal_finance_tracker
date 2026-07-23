"""
routes_investments.py — investments overview/transactions/transfer.

Split VERBATIM from app.py in Refactor Phase 5 (2026-07-04). No blueprints:
register(app, helpers) binds every view under its original function name, so
endpoint names, url_for(...) and base.html `ep ==` checks are unchanged.
"""
from datetime import datetime, date
from flask import (request, redirect, url_for,
                   render_template, jsonify, flash)
from config import CURRENT_YEAR, OWNER
from categories import INVESTMENT_ACCOUNTS
from db import get_db


def investments_overview():
    """Balance-sheet view + current-period contributions card.

    Transfer-flow conventions in this codebase:
      cash → invest single-sided pattern (e.g. Checking → Trad IRA):
        - One trx on the checking account, amount = -X, l2 = destination invest name
      invest → invest two-sided pattern (e.g. Trad IRA → Roth IRA):
        - Source row: account = source invest, amount = +X, l2 = source name
        - Dest row:   account = dest invest,   amount = -X, l2 = dest name

    Per-account flow rules used below:
      transfers_in  to X = sum |amount| where l2 = X.name AND amount < 0
                          (catches cash-side outflows landing in X
                           AND inter-invest dest rows landing in X)
      transfers_out from X = sum amount where account = X AND amount > 0
                          (inter-invest source rows leaving X)
    """
    from datetime import datetime as _dt
    year = request.args.get("year", str(_dt.now().year))
    db = get_db()

    accts = db.execute("""
        SELECT id, name, account_num, owner, provider, opening_balance, inv_group
        FROM accounts WHERE type='investment' AND active=1
        ORDER BY name
    """).fetchall()

    # Grouping is now the STORED inv_group field (user-editable); fall back to
    # the seed map (empty by default in this build) then 'Alternatives' for
    # any legacy/blank row.
    _seed_l1 = {row[0]: row[6] for row in INVESTMENT_ACCOUNTS}
    name_to_l1 = _seed_l1
    def _group_of(a):
        return (a["inv_group"] if "inv_group" in a.keys() else None) \
            or _seed_l1.get(a["name"]) or "Alternatives"

    # ── Lifetime balance contribution per account ───────────────────────
    # Sums all transfer trxs by l2_category (= account name). For inter-invest
    # transfers both rows share the same l2, so they cancel in the sum (net
    # change is zero, which is correct since balance just moved sideways).
    # For cash → invest transfers, only the cash-side row carries the l2,
    # so its negative amount, when negated, gives the inflow.
    transfer_totals = {}
    for r in db.execute("""
        SELECT l2_category, COALESCE(SUM(amount), 0) as total
        FROM transactions
        WHERE trx_type='transfer' AND status='active'
          AND l1_category IN ('Retirement','General Savings','Alternatives')
          AND l2_category IS NOT NULL
        GROUP BY l2_category
    """).fetchall():
        transfer_totals[r["l2_category"]] = -r["total"]

    # ── Per-account 2026 In / Out (for the table columns — shows ALL flows
    # including inter-invest moves, so the user can see e.g. "$10K in, $10K
    # out" of a Trad IRA that was rebalanced into a Roth) ───────────────
    in_2026 = {}
    for r in db.execute("""
        SELECT l2_category, COALESCE(SUM(ABS(amount)), 0) AS total
        FROM transactions
        WHERE trx_type='transfer' AND status='active'
          AND l1_category IN ('Retirement','General Savings','Alternatives')
          AND amount < 0
          AND strftime('%Y', trx_date) = ?
        GROUP BY l2_category
    """, (year,)).fetchall():
        in_2026[r["l2_category"]] = r["total"]

    out_2026 = {}
    for r in db.execute("""
        SELECT account_id, COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE trx_type='transfer' AND status='active' AND amount > 0
          AND strftime('%Y', trx_date) = ?
        GROUP BY account_id
    """, (year,)).fetchall():
        out_2026[r["account_id"]] = r["total"]

    # ── 2026 NET CONTRIBUTIONS card calc — true new capital deployed ─────
    # Excludes inter-invest moves (Trad IRA → Roth IRA is rebalancing,
    # not new contribution). Includes:
    #   cash → invest deposits (negative-amount transfers on non-invest accts)
    #   employer-match memos (tagged 'employer-contribution' on invest accts)
    contrib_cash_in = {}
    for r in db.execute("""
        SELECT t.l2_category, COALESCE(SUM(ABS(t.amount)), 0) AS total
          FROM transactions t
         WHERE t.trx_type='transfer' AND t.status='active' AND t.amount < 0
           AND t.l1_category IN ('Retirement','General Savings','Alternatives')
           AND strftime('%Y', t.trx_date) = ?
           AND t.account_id IN (SELECT id FROM accounts WHERE type != 'investment')
         GROUP BY t.l2_category
    """, (year,)).fetchall():
        contrib_cash_in[r["l2_category"]] = r["total"]

    contrib_cash_out = {}
    for r in db.execute("""
        SELECT t.l2_category, COALESCE(SUM(t.amount), 0) AS total
          FROM transactions t
         WHERE t.trx_type='transfer' AND t.status='active' AND t.amount > 0
           AND t.l1_category IN ('Retirement','General Savings','Alternatives')
           AND strftime('%Y', t.trx_date) = ?
           AND t.account_id IN (SELECT id FROM accounts WHERE type != 'investment')
         GROUP BY t.l2_category
    """, (year,)).fetchall():
        contrib_cash_out[r["l2_category"]] = r["total"]

    contrib_employer = {}
    for r in db.execute("""
        SELECT t.l2_category, COALESCE(SUM(ABS(t.amount)), 0) AS total
          FROM transactions t
          JOIN transaction_tags tt ON tt.trx_id = t.id
          JOIN tags tg ON tg.id = tt.tag_id
         WHERE tg.name = 'employer-contribution'
           AND t.trx_type='transfer' AND t.status='active'
           AND strftime('%Y', t.trx_date) = ?
         GROUP BY t.l2_category
    """, (year,)).fetchall():
        contrib_employer[r["l2_category"]] = r["total"]

    # ── Sum unrealized gains / dividends / interest / fees per account ──
    adj_totals = {}
    for r in db.execute("""
        SELECT account_id, COALESCE(SUM(amount), 0) as total
        FROM investment_adjustments GROUP BY account_id
    """).fetchall():
        adj_totals[r["account_id"]] = r["total"]

    # ── Employer contributions in {year} (via the 'employer-contribution' tag) ──
    # Same data as contrib_employer above (dict {account_name: total}). The old
    # scalar version filtered amount>0, but these transfers are stored negative
    # (inflow into the investment account), so it always returned 0.
    employer_contrib_2026 = dict(contrib_employer)

    def _z(x):
        """Floor floating-point near-zero to clean 0.0 (kills -0.0 artifact
        from e.g. \$10,000 in then \$10,000 out)."""
        return 0.0 if abs(x) < 0.005 else x

    # Group by L1
    groups = {"Retirement": [], "General Savings": [], "Alternatives": []}
    grand_value = 0.0
    grand_invested = 0.0
    for a in accts:
        l1 = _group_of(a)
        transfers_in     = _z(transfer_totals.get(a["name"], 0.0))
        adjustments      = _z(adj_totals.get(a["id"], 0.0))
        invested         = _z((a["opening_balance"] or 0.0) + transfers_in)
        value            = _z(invested + adjustments)
        period_in        = _z(in_2026.get(a["name"], 0.0))
        period_out       = _z(out_2026.get(a["id"], 0.0))
        # period_net = ALL flows incl. inter-invest (rebalance shows up here)
        period_net       = _z(period_in - period_out)
        # contrib_net = own-pocket capital only (cash deposits − cash withdrawals).
        # Excludes inter-invest moves AND employer match contributions —
        # employer match is "free money", tracked separately if needed.
        contrib_net      = _z(
            contrib_cash_in.get(a["name"], 0.0)
            - contrib_cash_out.get(a["name"], 0.0)
        )
        gain_loss        = adjustments  # gain over invested principal
        groups.setdefault(l1, []).append({
            "id":              a["id"],
            "name":            a["name"],
            "owner":           a["owner"],
            "provider":        a["provider"] or "—",
            "opening_balance": a["opening_balance"] or 0.0,
            "transfers_in_lifetime": transfers_in,
            "transfers_in":    period_in,
            "transfers_out":   period_out,
            "period_net":      period_net,
            "contrib_net":     contrib_net,   # ← used by Contributions card
            "adjustments":     adjustments,
            "gain_loss":       gain_loss,
            "invested":        invested,
            "value":           value,
            "balance":         value,
        })
        grand_value    += value
        grand_invested += invested

    group_totals = {
        l1: {"value":       sum(a["value"]      for a in items),
             "invested":    sum(a["invested"]   for a in items),
             "period_in":   sum(a["transfers_in"]  for a in items),
             "period_out":  sum(a["transfers_out"] for a in items),
             "period_net":  sum(a["period_net"]    for a in items),
             "contrib_net": sum(a["contrib_net"]   for a in items)}
        for l1, items in groups.items()
    }

    # ── Performance layer (lot engine, 2026-07-05) ──────────────────────
    # Populated once history is loaded (CSV import / ledger / sync). Until
    # events exist the overview shows an onboarding pointer instead.
    import investments_engine as _ieng
    import investments_returns as _iret
    perf = None
    n_events = db.execute("SELECT COUNT(*) FROM investment_events").fetchone()[0]
    if n_events:
        inv_accts = db.execute(
            "SELECT * FROM accounts WHERE type='investment' ORDER BY active DESC, name"
        ).fetchall()
        show_closed = request.args.get("closed") == "1"
        all_rows = [_account_summary(db, a) for a in inv_accts]
        def _has_activity(r):
            return bool(r["value"] > 0.005 or r["principal"] > 0.005 or r["n_lots"]
                or db.execute("SELECT 1 FROM investment_events WHERE account_id=? LIMIT 1",
                              (r["acct"]["id"],)).fetchone())
        all_rows = [r for r in all_rows if _has_activity(r)]
        n_closed = sum(1 for r in all_rows if not r["acct"]["active"])
        # Closed accounts are hidden by default; the toggle reveals them.
        rows_p = all_rows if show_closed else [r for r in all_rows if r["acct"]["active"]]
        # Portfolio totals always reflect ALL accounts (closed ones still hold
        # value in the engine); only the table's visibility changes.
        total_value = sum(r["value"] for r in all_rows)
        gflows = _iret.global_flows(db)
        total_in = sum(-a for _, a in gflows if a < 0)
        total_out = sum(a for _, a in gflows if a > 0)
        series = _iret.portfolio_series(db, total_value)
        contrib = _iret.contributions_series(db)
        employer = _iret.employer_summary(db)
        net_in = total_in - total_out
        # This-year money in, split your-capital vs employer (source lives on the
        # lot each contribution event created).
        yr_you = db.execute("""SELECT COALESCE(SUM(e.amount),0) FROM investment_events e
            JOIN investment_lots l ON l.id=e.lot_id
            WHERE e.kind='contribution' AND l.source='you'
              AND strftime('%Y',e.event_date)=?""", (year,)).fetchone()[0]
        yr_wd = db.execute("""SELECT COALESCE(SUM(ABS(amount)),0) FROM investment_events
            WHERE kind='withdrawal' AND strftime('%Y',event_date)=?""", (year,)).fetchone()[0]
        yr_emp = db.execute("""SELECT COALESCE(SUM(e.amount),0) FROM investment_events e
            JOIN investment_lots l ON l.id=e.lot_id
            WHERE e.kind='contribution' AND l.source='employer'
              AND strftime('%Y',e.event_date)=?""", (year,)).fetchone()[0]
        # Group the per-account rows by L1 for per-group tables + subtotals.
        groups_perf = []
        for _l1 in ("Retirement", "General Savings", "Alternatives"):
            _grp = [r for r in rows_p if (r["acct"]["inv_group"] or "Alternatives") == _l1]
            if _grp:
                groups_perf.append({"l1": _l1, "rows": _grp,
                    "value": sum(r["value"] for r in _grp),
                    "principal": sum(r["principal"] for r in _grp),
                    "gain": sum(r["gain"] for r in _grp)})
        grand = {"value": sum(r["value"] for r in rows_p),
                 "principal": sum(r["principal"] for r in rows_p),
                 "gain": sum(r["value"] - r["principal"] for r in rows_p)}
        perf = {
            "rows": rows_p,
            "total_value": total_value,
            "total_in": total_in, "total_out": total_out,
            "net_in": net_in,
            "your_capital": net_in - employer["given"],  # excl. employer money
            "employer": employer,                        # rollup (given/grown/by year)
            "year_you": yr_you - yr_wd, "year_emp": yr_emp,
            "groups_perf": groups_perf, "grand": grand,
            "gain": total_value - net_in,                # true market gain (unchanged)
            "xirr": _iret.global_xirr(db, total_value),
            "series": series, "contrib": contrib,
            "show_closed": show_closed, "n_closed": n_closed,
        }

    return render_template("investments/overview.html",
        year=year,
        groups=groups, group_totals=group_totals,
        grand_total=grand_value, grand_invested=grand_invested,
        employer_contrib_2026=employer_contrib_2026,
        perf=perf,
    )


def investments_transfer():
    """Record a transfer between two investment accounts (e.g., Trad IRA →
    Roth IRA, or closing a CD into a brokerage). Creates two coordinated
    `transactions` rows so both account balances update correctly.

    Sign convention (matches the existing transfer model):
      - Source-side row: l2=source name, amount=+X (positive = outflow)
      - Dest-side row:   l2=dest name,   amount=-X (negative = inflow)
    The Investments Overview sums by L2 and negates to derive the
    investment-account-side balance impact, which works for both rows.

    NOTE: This is an interim model. The real fix is the `investment_lots`
    table (see docs/handbook.html §12 and /investments/docs), which carries
    principal vs gain across inter-investment moves correctly. For now
    a transfer is recorded as a single principal amount on both sides.
    """
    db = get_db()
    accts = db.execute("""
        SELECT id, name, account_num, owner, provider
        FROM accounts WHERE type='investment' AND active=1
        ORDER BY name
    """).fetchall()

    name_to_l1 = {row[0]: row[6] for row in INVESTMENT_ACCOUNTS}

    if request.method == "POST":
        import hashlib
        from datetime import date
        try:
            src_id = int(request.form.get("source_account_id") or 0)
            dst_id = int(request.form.get("dest_account_id")   or 0)
            amount = float(request.form.get("amount") or 0)
        except (TypeError, ValueError):
            flash("Invalid input.", "error")
            return redirect(url_for("investments_transfer"))
        trx_date = request.form.get("trx_date") or date.today().isoformat()
        note     = (request.form.get("note") or "").strip() or None

        if src_id == dst_id or amount <= 0 or src_id == 0 or dst_id == 0:
            flash("Source and destination must differ; amount must be > 0.", "error")
            return redirect(url_for("investments_transfer"))

        src = db.execute("SELECT * FROM accounts WHERE id=?", (src_id,)).fetchone()
        dst = db.execute("SELECT * FROM accounts WHERE id=?", (dst_id,)).fetchone()
        if not src or not dst:
            flash("Account not found.", "error")
            return redirect(url_for("investments_transfer"))

        src_l1 = name_to_l1.get(src["name"], "Alternatives")
        dst_l1 = name_to_l1.get(dst["name"], "Alternatives")

        ddk_base = f"{trx_date}|{src['name']}|{dst['name']}|{amount:.2f}"
        ddk_src  = hashlib.md5(("OUT|" + ddk_base).encode()).hexdigest()
        ddk_dst  = hashlib.md5(("IN|"  + ddk_base).encode()).hexdigest()

        # Source-side row: outflow from source investment account
        db.execute("""
            INSERT INTO transactions
              (account_id, trx_date, raw_description, vendor, amount, trx_type,
               owner, l1_category, l2_category, note, dedup_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (src_id, trx_date,
              f"Inter-investment transfer to {dst['name']}",
              f"→ {dst['name']}",
              amount, "transfer", src["owner"],
              src_l1, src["name"], note, ddk_src))

        # Dest-side row: inflow to destination investment account
        db.execute("""
            INSERT INTO transactions
              (account_id, trx_date, raw_description, vendor, amount, trx_type,
               owner, l1_category, l2_category, note, dedup_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (dst_id, trx_date,
              f"Inter-investment transfer from {src['name']}",
              f"← {src['name']}",
              -amount, "transfer", dst["owner"],
              dst_l1, dst["name"], note, ddk_dst))

        db.commit()
        flash(f"Transferred ${amount:,.2f} from {src['name']} to {dst['name']}.", "success")
        return redirect(url_for("investments_overview"))

    from datetime import date
    return render_template("investments/transfer.html",
        accts=accts, name_to_l1=name_to_l1, today=date.today().isoformat()
    )


def investments_transactions():
    """RETIRED (2026-07-06): the old transfer-trx list is merged into the
    Ledger, which is now the single 'Transactions' view (engine events are the
    source of truth). Kept as a permanent redirect so old links keep working."""
    return redirect(url_for("investments_ledger"))


def _investments_transactions_legacy():
    """Dead code — the pre-merge implementation, retained for reference only."""
    db   = get_db()
    year = request.args.get("year", CURRENT_YEAR)
    q    = request.args.get("q", "").strip()
    acct = request.args.get("acct", "").strip()

    filters = ["t.trx_type='transfer'", "t.status='active'",
               "t.l1_category IN ('Retirement','General Savings','Alternatives')",
               "strftime('%Y', t.trx_date)=?"]
    params  = [year]
    if acct:
        filters.append("t.account_id=?"); params.append(acct)
    if q:
        # Match vendor / raw description / note / any tag name (substring).
        filters.append(
            "(t.vendor LIKE ? OR t.raw_description LIKE ? OR t.note LIKE ? "
            "OR EXISTS (SELECT 1 FROM transaction_tags tt "
            "JOIN tags tg ON tg.id = tt.tag_id "
            "WHERE tt.trx_id = t.id AND tg.name LIKE ?))"
        )
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])
    where = " AND ".join(filters)

    rows = db.execute(f"""
        SELECT t.*, a.name as account_name
        FROM transactions t JOIN accounts a ON t.account_id=a.id
        WHERE {where}
        ORDER BY t.trx_date DESC, t.id DESC
    """, params).fetchall()

    total = sum(r["amount"] for r in rows)

    # Accounts that actually hold investment transfers — for the filter dropdown.
    accounts = db.execute("""
        SELECT DISTINCT a.id, a.name
        FROM transactions t JOIN accounts a ON t.account_id=a.id
        WHERE t.trx_type='transfer' AND t.status='active'
          AND t.l1_category IN ('Retirement','General Savings','Alternatives')
        ORDER BY a.name
    """).fetchall()

    available_years = db.execute("""
        SELECT DISTINCT strftime('%Y', trx_date) as yr
        FROM transactions WHERE trx_type='transfer' AND status='active'
        ORDER BY yr DESC
    """).fetchall()

    return render_template("investments/transactions.html",
        rows=rows, year=year, total=total, q=q, acct=acct, accounts=accounts,
        available_years=[r["yr"] for r in available_years] or [year],
    )


# ═══ Lot-engine layer (2026-07-05 build) ═════════════════════════════════════
# Everything below rides on investments_engine (lots + events) +
# investments_returns (XIRR/TWR/lot metrics). See docs/handbook.html §12.

import investments_engine as ieng
import investments_returns as iret

# The three canonical L1 groups the Overview renders. Add-account / meta pick
# from these; anything else falls through to Alternatives on the Overview.
INV_GROUPS = ["Retirement", "General Savings", "Alternatives"]


def _clean_group(v):
    v = (v or "").strip()
    return v if v in INV_GROUPS else "General Savings"


def _inv_accounts(db, include_inactive=True):
    q = "SELECT * FROM accounts WHERE type='investment'"
    if not include_inactive:
        q += " AND active=1"
    return db.execute(q + " ORDER BY active DESC, name").fetchall()


def _acct_by_name(db):
    return {r["name"]: r for r in _inv_accounts(db)}


def _account_summary(db, a):
    """One account's engine-derived numbers for tables/cards."""
    value = ieng.account_value(db, a["id"])
    principal = ieng.account_principal(db, a["id"])
    n_lots = db.execute("""SELECT COUNT(*) FROM investment_lots
        WHERE current_account_id=? AND closed_at IS NULL""", (a["id"],)).fetchone()[0]
    last_snap = db.execute("""SELECT event_date, snapshot_value FROM investment_events
        WHERE account_id=? AND kind='snapshot'
        ORDER BY event_date DESC, id DESC LIMIT 1""", (a["id"],)).fetchone()
    x = iret.account_xirr(db, a["id"], value)
    twr = iret.account_twr(db, a["id"])
    return {"acct": a, "value": value, "principal": principal,
            "gain": value - principal, "n_lots": n_lots,
            "xirr": x, "twr": twr,
            "last_snap": dict(last_snap) if last_snap else None}


def investments_account(account_id):
    """Per-account detail: metadata editor, lots (the 'each $10K' view),
    event ledger, quick entry forms."""
    db = get_db()
    a = db.execute("SELECT * FROM accounts WHERE id=? AND type='investment'",
                   (account_id,)).fetchone()
    if not a:
        flash("Not an investment account.", "error")
        return redirect("/investments/overview")
    summ = _account_summary(db, a)
    show_closed = request.args.get("closed") == "1"
    lots = db.execute(f"""
        SELECT * FROM investment_lots
         WHERE current_account_id=? {"" if show_closed else "AND closed_at IS NULL"}
         ORDER BY origin_date, id""", (account_id,)).fetchall()
    lots = [dict(l) | iret.lot_metrics(l) for l in lots]
    events = db.execute("""
        SELECT e.*, a2.name AS acct_name FROM investment_events e
        JOIN accounts a2 ON a2.id = e.account_id
        WHERE e.account_id=? ORDER BY e.event_date DESC, e.id DESC LIMIT 200
    """, (account_id,)).fetchall()
    accounts = _inv_accounts(db)
    return render_template("investments/account.html",
        a=a, s=summ, lots=lots, events=events, accounts=accounts,
        show_closed=show_closed)


def api_inv_account_meta(account_id):
    """Save account metadata (name / provider / notes / dates / active).
    Renaming also renames the transfer L2 category rows so the checking-
    side transfer convention keeps working."""
    db = get_db()
    a = db.execute("SELECT * FROM accounts WHERE id=? AND type='investment'",
                   (account_id,)).fetchone()
    if not a:
        return jsonify({"ok": False, "error": "not an investment account"}), 404
    data = request.get_json(silent=True) or {}
    new_name = (data.get("name") or a["name"]).strip()
    if new_name != a["name"]:
        if db.execute("SELECT 1 FROM accounts WHERE name=? AND id != ?",
                      (new_name, account_id)).fetchone():
            return jsonify({"ok": False, "error": "name already in use"}), 400
        # Keep the transfer-category convention intact (L2 == account name).
        db.execute("UPDATE categories SET l2=? WHERE l2=? AND trx_type='transfer'",
                   (new_name, a["name"]))
        db.execute("""UPDATE transactions SET l2_category=?
                      WHERE l2_category=? AND trx_type='transfer'""",
                   (new_name, a["name"]))
    # Group (L1) is editable; owner is NOT — single-owner build, so we pin
    # owner=OWNER and expose no owner field.
    group = _clean_group(data.get("group")) if data.get("group") is not None \
        else (a["inv_group"] or "General Savings")
    db.execute(f"""UPDATE accounts SET name=?, provider=?, notes=?,
                  opened_date=?, closed_date=?, active=?, inv_group=?, owner='{OWNER}' WHERE id=?""",
               (new_name, (data.get("provider") or "").strip() or None,
                (data.get("notes") or "").strip() or None,
                (data.get("opened_date") or "").strip() or None,
                (data.get("closed_date") or "").strip() or None,
                1 if data.get("active", a["active"]) in (1, "1", True, "true") else 0,
                group,
                account_id))
    # Keep the transfer category's L1 aligned with the account's group so the
    # checking-side transfer picker files new transfers under the right L1.
    db.execute("""UPDATE categories SET l1=? WHERE l2=? AND trx_type='transfer'""",
               (group, new_name))
    db.commit()
    return jsonify({"ok": True, "name": new_name})


def api_inv_account_add():
    """Create a new investment account. Body: {name, group, provider,
    opened_date, notes}. group ∈ Retirement / General Savings / Alternatives.
    Owner is always the single configured OWNER."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    if db.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
        return jsonify({"ok": False, "error": "name already in use"}), 400
    group = _clean_group(data.get("group"))
    num = "inv-" + "".join(c for c in name.lower().replace(" ", "-")
                           if c.isalnum() or c == "-")[:24]
    n, base = 2, num
    while db.execute("SELECT 1 FROM accounts WHERE account_num=?", (num,)).fetchone():
        num = f"{base}{n}"; n += 1
    cur = db.execute(f"""INSERT INTO accounts (name, account_num, type, owner,
                        active, opening_balance, provider, notes, opened_date, inv_group)
                        VALUES (?,?, 'investment', '{OWNER}', 1, 0, ?, ?, ?, ?)""",
                     (name, num,
                      (data.get("provider") or "").strip() or None,
                      (data.get("notes") or "").strip() or None,
                      (data.get("opened_date") or "").strip() or None,
                      group))
    acct_id = cur.lastrowid
    # Transfer L2 so checking-side transfers can target it, filed under the
    # account's chosen L1 group.
    db.execute("""INSERT OR IGNORE INTO categories (trx_type, l1, l2)
                  VALUES ('transfer', ?, ?)""", (group, name))
    db.commit()
    return jsonify({"ok": True, "id": acct_id})


def investments_ledger():
    """Chronological all-events feed + entry forms for every event type +
    the checking-transfer sync."""
    db = get_db()
    # Multi-select filters (checkbox dropdowns, like income/expenses).
    sel_accts = [a for a in request.args.getlist("accts") if a]
    sel_kinds = [k for k in request.args.getlist("kinds") if k]
    where, params = ["1=1"], []
    if sel_accts:
        where.append("e.account_id IN (%s)" % ",".join("?" * len(sel_accts)))
        params += sel_accts
    if sel_kinds:
        where.append("e.kind IN (%s)" % ",".join("?" * len(sel_kinds)))
        params += sel_kinds
    events = db.execute(f"""
        SELECT e.*, a.name AS acct_name FROM investment_events e
        JOIN accounts a ON a.id = e.account_id
        WHERE {' AND '.join(where)}
        ORDER BY e.event_date DESC, e.id DESC LIMIT 500
    """, params).fetchall()
    # Pending transfers not yet synced into the engine — the checking-
    # side contributions/withdrawals PLUS employer-contribution rows that sit
    # on the investment account itself (both are handled by /sync-transfers).
    pending_sync = db.execute("""
        SELECT COUNT(*) FROM transactions t
         WHERE t.trx_type='transfer' AND t.status='active'
           AND t.id NOT IN (SELECT linked_trx_id FROM investment_events
                             WHERE linked_trx_id IS NOT NULL)
           AND (
             ( t.l1_category IN ('Retirement','General Savings','Alternatives')
               AND t.l2_category IN (SELECT name FROM accounts WHERE type='investment')
               AND t.account_id NOT IN (SELECT id FROM accounts WHERE type='investment') )
             OR
             ( t.account_id IN (SELECT id FROM accounts WHERE type='investment')
               AND t.id IN (SELECT tt.trx_id FROM transaction_tags tt
                            JOIN tags tg ON tg.id=tt.tag_id
                            WHERE tg.name='employer-contribution') )
           )
    """).fetchone()[0]
    accts_all = _inv_accounts(db)
    return render_template("investments/ledger.html",
        events=events, accounts=accts_all,
        acct_options=[{"v": a["id"], "t": a["name"]} for a in accts_all],
        kind_options=['contribution', 'withdrawal', 'lot_move_in', 'lot_move_out',
                      'snapshot', 'dividend', 'interest', 'fee', 'gain_loss', 'closure'],
        selected_accts=sel_accts, selected_kinds=sel_kinds,
        pending_sync=pending_sync)


def api_inv_event_note(event_id):
    """Live-edit an event's note from the Transactions table."""
    db = get_db()
    d = request.get_json(silent=True) or {}
    note = (d.get("note") or "").strip() or None
    db.execute("UPDATE investment_events SET note=? WHERE id=?", (note, event_id))
    db.commit()
    return jsonify({"ok": True})


def api_inv_event_add():
    """One endpoint for every manual event type. Body:
    {type, date, account_id, amount?, to_account_id?, value?, note?}
    type ∈ contribution | withdrawal | move | snapshot | dividend |
           interest | fee | gain_loss | close"""
    db = get_db()
    d = request.get_json(silent=True) or {}
    typ = d.get("type")
    dte = (d.get("date") or "").strip()
    note = (d.get("note") or "").strip() or None
    try:
        acct = int(d.get("account_id") or 0)
    except (TypeError, ValueError):
        acct = 0
    if not dte or len(dte) != 10:
        return jsonify({"ok": False, "error": "date (YYYY-MM-DD) required"}), 400

    def _amt():
        try:
            return float(d.get("amount"))
        except (TypeError, ValueError):
            raise ValueError("numeric amount required")

    try:
        if typ == "contribution":
            _src = "employer" if (d.get("source") or "").strip().lower() \
                in ("employer", "emp", "match") else "you"
            ieng.record_contribution(db, account_id=acct, event_date=dte,
                                     amount=_amt(), note=note, source=_src)
        elif typ == "withdrawal":
            ieng.record_withdrawal(db, account_id=acct, event_date=dte,
                                   amount=_amt(), note=note)
        elif typ == "move":
            ieng.record_lot_move(db, src_account_id=acct,
                                 dst_account_id=int(d.get("to_account_id") or 0),
                                 event_date=dte, amount=_amt(), note=note)
        elif typ == "snapshot":
            ieng.record_snapshot(db, account_id=acct, event_date=dte,
                                 snapshot_value=float(d.get("value")), note=note)
        elif typ in ("dividend", "interest", "fee", "gain_loss"):
            ieng.record_adjustment(db, account_id=acct, event_date=dte,
                                   kind=typ, amount=_amt(), note=note)
        elif typ == "close":
            dest = d.get("to_account_id")
            ieng.record_closure(db, account_id=acct, event_date=dte,
                                dest_account_id=int(dest) if dest else None,
                                note=note)
            db.execute("UPDATE accounts SET closed_date=? WHERE id=?", (dte, acct))
        else:
            return jsonify({"ok": False, "error": f"unknown type {typ}"}), 400
    except (ValueError, TypeError) as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 400
    db.commit()
    return jsonify({"ok": True})


def _sync_investment_events(db):
    """Core sync: pull approved investment-transfer trxs into the engine as
    contribution/withdrawal events (idempotent via linked_trx_id). Returns
    (done, skipped). Does NOT commit — the caller owns the transaction. Uses a
    per-row SAVEPOINT so one bad row (e.g. a withdrawal that exceeds tracked
    lots) rolls back only itself, never the caller's other work. This is the
    shared engine used by both the manual Sync button and the auto-sync hook
    (review approve)."""
    def _try(fn, trx_id, out):
        db.execute("SAVEPOINT sync_row")
        try:
            fn(); db.execute("RELEASE SAVEPOINT sync_row"); out[0] += 1
        except ValueError as e:
            db.execute("ROLLBACK TO SAVEPOINT sync_row")
            db.execute("RELEASE SAVEPOINT sync_row")
            out[1].append({"trx": trx_id, "why": str(e)[:120]})

    n = [0, []]
    by_name = _acct_by_name(db)
    # checking-side contributions/withdrawals
    for t in db.execute("""
        SELECT t.* FROM transactions t
         WHERE t.trx_type='transfer' AND t.status='active'
           AND t.l1_category IN ('Retirement','General Savings','Alternatives')
           AND t.l2_category IN (SELECT name FROM accounts WHERE type='investment')
           AND t.account_id NOT IN (SELECT id FROM accounts WHERE type='investment')
           AND t.id NOT IN (SELECT linked_trx_id FROM investment_events
                             WHERE linked_trx_id IS NOT NULL)
         ORDER BY t.trx_date, t.id""").fetchall():
        dest = by_name.get(t["l2_category"])
        if not dest:
            n[1].append({"trx": t["id"], "why": f"no account named {t['l2_category']}"}); continue
        amt = float(t["amount"])
        if amt < 0:
            _try(lambda t=t, dest=dest, amt=amt: ieng.record_contribution(
                db, account_id=dest["id"], event_date=t["trx_date"], amount=abs(amt),
                linked_trx_id=t["id"], source="you",
                note=f"Synced from trx #{t['id']} ({(t['vendor'] or t['raw_description'])[:30]})"), t["id"], n)
        else:
            _try(lambda t=t, dest=dest, amt=amt: ieng.record_withdrawal(
                db, account_id=dest["id"], event_date=t["trx_date"], amount=amt,
                linked_trx_id=t["id"], note=f"Synced from trx #{t['id']}"), t["id"], n)
    # employer matches on the investment account (tagged)
    for t in db.execute("""
        SELECT t.* FROM transactions t
          JOIN transaction_tags tt ON tt.trx_id = t.id
          JOIN tags tg ON tg.id = tt.tag_id
         WHERE tg.name = 'employer-contribution'
           AND t.trx_type='transfer' AND t.status='active'
           AND t.account_id IN (SELECT id FROM accounts WHERE type='investment')
           AND t.id NOT IN (SELECT linked_trx_id FROM investment_events
                             WHERE linked_trx_id IS NOT NULL)
         ORDER BY t.trx_date, t.id""").fetchall():
        _try(lambda t=t: ieng.record_contribution(
            db, account_id=t["account_id"], event_date=t["trx_date"],
            amount=abs(float(t["amount"])), linked_trx_id=t["id"], source="employer",
            note=f"Employer contribution — synced from trx #{t['id']}"), t["id"], n)
    return n[0], n[1]


def auto_sync_after_change(db):
    """Auto-sync hook — called after an investment transfer is approved/booked
    so lot events appear WITHOUT the manual Sync button. Safe to call after any
    approve; it's idempotent and only touches unlinked investment transfers."""
    try:
        _sync_investment_events(db)
    except Exception:
        pass  # never let a sync hiccup break the approve/booking flow


def auto_unsync_trx(db, trx_id):
    """Reverse hook — when a linked investment transfer is deleted, remove the
    lot event it created so the engine stays consistent. Only auto-reverses a
    PRISTINE contribution (its lot has no children and hasn't been touched by a
    snapshot/adjustment, i.e. current_value == origin_amount). Anything more
    entangled (already moved/split, or a withdrawal) is left for a manual
    re-sync/rebuild rather than risk a bad partial reversal. Guarded — never
    breaks the delete flow."""
    try:
        for e in db.execute("SELECT id, kind, lot_id FROM investment_events "
                            "WHERE linked_trx_id=?", (trx_id,)).fetchall():
            if e["kind"] == "contribution" and e["lot_id"]:
                lot = db.execute("SELECT origin_amount, current_value FROM investment_lots "
                                 "WHERE id=?", (e["lot_id"],)).fetchone()
                child = db.execute("SELECT 1 FROM investment_lots WHERE parent_lot_id=? LIMIT 1",
                                   (e["lot_id"],)).fetchone()
                pristine = (lot and not child and
                            abs(float(lot["current_value"]) - float(lot["origin_amount"])) < 0.005)
                if pristine:
                    # break the circular FK (lot.origin_event_id -> event), then
                    # delete the event, then the lot.
                    db.execute("UPDATE investment_lots SET origin_event_id=NULL WHERE id=?", (e["lot_id"],))
                    db.execute("DELETE FROM investment_events WHERE id=?", (e["id"],))
                    db.execute("DELETE FROM investment_lots WHERE id=?", (e["lot_id"],))
    except Exception:
        pass


def api_inv_sync_trx():
    """Manual Sync button — same engine as the auto-sync, kept as a backfill /
    repair tool (e.g. a one-time historical catch-up, or after edits)."""
    db = get_db()
    done, skipped = _sync_investment_events(db)
    db.commit()
    return jsonify({"ok": True, "synced": done, "skipped": skipped})


def _unused_old_sync():
    db = get_db()
    rows = db.execute("""
        SELECT t.* FROM transactions t
         WHERE t.trx_type='transfer' AND t.status='active'
           AND t.l1_category IN ('Retirement','General Savings','Alternatives')
           AND t.l2_category IN (SELECT name FROM accounts WHERE type='investment')
           AND t.account_id NOT IN (SELECT id FROM accounts WHERE type='investment')
           AND t.id NOT IN (SELECT linked_trx_id FROM investment_events
                             WHERE linked_trx_id IS NOT NULL)
         ORDER BY t.trx_date, t.id
    """).fetchall()
    by_name = _acct_by_name(db)
    done, skipped = 0, []
    for t in rows:
        dest = by_name.get(t["l2_category"])
        if not dest:
            skipped.append({"trx": t["id"], "why": f"no account named {t['l2_category']}"})
            continue
        amt = float(t["amount"])
        try:
            if amt < 0:      # money left checking → contribution (your money)
                ieng.record_contribution(db, account_id=dest["id"],
                    event_date=t["trx_date"], amount=abs(amt),
                    linked_trx_id=t["id"], source="you",
                    note=f"Synced from trx #{t['id']} ({(t['vendor'] or t['raw_description'])[:30]})")
            else:            # money returned to checking → withdrawal
                ieng.record_withdrawal(db, account_id=dest["id"],
                    event_date=t["trx_date"], amount=amt,
                    linked_trx_id=t["id"],
                    note=f"Synced from trx #{t['id']}")
            done += 1
        except ValueError as e:
            db.rollback()
            skipped.append({"trx": t["id"], "why": str(e)[:120]})

    # ── Employer-contribution pass ──────────────────────────────────────────
    # Employer matches land as transfer rows ON the investment account itself
    # (not the checking side), tagged 'employer-contribution' — so the query
    # above (which requires a non-investment account) skips them. Pull them in
    # here as source='employer' contributions, idempotent via linked_trx_id.
    # Rows on investment accounts WITHOUT the tag are inter-investment move legs
    # and are deliberately left for manual `move` entry.
    emp_rows = db.execute("""
        SELECT t.* FROM transactions t
          JOIN transaction_tags tt ON tt.trx_id = t.id
          JOIN tags tg ON tg.id = tt.tag_id
         WHERE tg.name = 'employer-contribution'
           AND t.trx_type='transfer' AND t.status='active'
           AND t.account_id IN (SELECT id FROM accounts WHERE type='investment')
           AND t.id NOT IN (SELECT linked_trx_id FROM investment_events
                             WHERE linked_trx_id IS NOT NULL)
         ORDER BY t.trx_date, t.id
    """).fetchall()
    for t in emp_rows:
        try:
            ieng.record_contribution(db, account_id=t["account_id"],
                event_date=t["trx_date"], amount=abs(float(t["amount"])),
                linked_trx_id=t["id"], source="employer",
                note=f"Employer contribution — synced from trx #{t['id']}")
            done += 1
        except ValueError as e:
            db.rollback()
            skipped.append({"trx": t["id"], "why": str(e)[:120]})

    db.commit()
    return jsonify({"ok": True, "synced": done, "skipped": skipped})


# ─── CSV import (one-time historical backfill) ───────────────────────────

_CSV_TYPES = {"contribution", "withdrawal", "move", "snapshot",
              "dividend", "interest", "fee", "gain_loss", "close"}


def investments_csv_template():
    from flask import Response
    sample = (
        "date,account,type,amount,to_account,value,source,note\n"
        "2020-01-15,Sample CD,contribution,1000,,,,first deposit (blank source = your money)\n"
        "2021-01-02,Sample CD,snapshot,,,1040,,statement value\n"
        "2022-06-30,Sample CD,snapshot,,,1080,,CD matured\n"
        "2022-07-01,Sample CD,move,1080,Sample Brokerage,,,rolled matured CD into brokerage\n"
        "2022-07-02,Sample CD,close,,,,,account closed after rollover\n"
        "2023-01-15,Roth IRA,contribution,2000,,,,prior-year contribution\n"
        "2023-03-31,My 401K,contribution,100,,,employer,employer match (free money)\n"
        "2024-02-20,Sample Brokerage,withdrawal,500,,,,cash needed\n"
        "2025-12-31,Sample Brokerage,snapshot,,,5000,,year-end statement\n"
    )
    return Response(sample, mimetype="text/csv",
        headers={"Content-Disposition":
                 "attachment; filename=investments_ledger_template.csv"})


def _read_tabular(file_storage):
    """Read a CSV **or XLSX** upload → (fieldnames, records). Every cell comes
    back as a trimmed string (dates as YYYY-MM-DD), so the two formats feed the
    exact same downstream parser."""
    fn = (file_storage.filename or "").lower()
    if fn.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError:
            raise ValueError("XLSX import needs the openpyxl package "
                             "(pip install openpyxl) — or save the sheet as CSV.")
        import io as _io
        from datetime import datetime as _dt, date as _date
        wb = openpyxl.load_workbook(_io.BytesIO(file_storage.read()),
                                    data_only=True, read_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        header = next(it, None) or []
        fields = [str(c).strip() if c is not None else "" for c in header]

        def _cell(v):
            if v is None:
                return ""
            if isinstance(v, (_dt, _date)):
                return v.strftime("%Y-%m-%d")
            if isinstance(v, float) and v.is_integer():
                return str(int(v))
            return str(v).strip()

        recs = []
        for raw in it:
            if raw is None or not any(v not in (None, "") for v in raw):
                continue
            recs.append({fields[i]: _cell(raw[i]) for i in range(len(fields))
                         if fields[i] and i < len(raw)})
        return fields, recs
    # CSV (BOM-tolerant)
    import csv, io
    text = file_storage.read().decode("utf-8-sig", errors="replace")
    rd = csv.DictReader(io.StringIO(text))
    return (rd.fieldnames or []), list(rd)


def _parse_inv_csv(db, file_storage):
    """→ (rows, errors). Each parsed row: {n, date, account_id, account,
    type, amount, to_account_id, to_account, value, source, note}. Accepts a
    CSV or an XLSX upload (see _read_tabular)."""
    by_name = _acct_by_name(db)
    rows, errors = [], []
    try:
        fieldnames, records = _read_tabular(file_storage)
    except ValueError as e:
        return [], [str(e)]
    missing = [c for c in ("date", "account", "type")
               if c not in (fieldnames or [])]
    if missing:
        return [], [f"File is missing required column(s): {', '.join(missing)}"]
    for n, r in enumerate(records, start=2):    # header is line 1
        dte = (r.get("date") or "").strip()
        acct_name = (r.get("account") or "").strip()
        typ = (r.get("type") or "").strip().lower()
        err = []
        if len(dte) != 10:
            err.append("bad date (want YYYY-MM-DD)")
        if typ not in _CSV_TYPES:
            err.append(f"bad type '{typ}'")
        acct = by_name.get(acct_name)
        if not acct:
            err.append(f"unknown account '{acct_name}' (create it first on the Overview)")
        amount = to_id = value = None
        to_name = (r.get("to_account") or "").strip()
        if typ in ("contribution", "withdrawal", "move",
                   "dividend", "interest", "fee", "gain_loss"):
            try:
                amount = float((r.get("amount") or "").replace(",", "").replace("$", ""))
            except ValueError:
                err.append("numeric amount required")
        if typ == "move":
            to = by_name.get(to_name)
            if not to:
                err.append(f"unknown to_account '{to_name}'")
            else:
                to_id = to["id"]
        if typ == "close" and to_name:
            to = by_name.get(to_name)
            if not to:
                err.append(f"unknown to_account '{to_name}'")
            else:
                to_id = to["id"]
        if typ == "snapshot":
            try:
                value = float((r.get("value") or "").replace(",", "").replace("$", ""))
            except ValueError:
                err.append("numeric value required for snapshot")
        # SOURCE — 'employer' marks free money that came straight from an
        # employer; anything else (incl. blank) is your own capital ('you').
        # Only meaningful on contributions; ignored elsewhere.
        src_raw = (r.get("source") or "").strip().lower()
        source = "employer" if src_raw in ("employer", "emp", "match") else "you"
        if src_raw and src_raw not in ("employer", "emp", "match",
                                       "you", "me", "mine", "self", "own"):
            err.append(f"bad source '{src_raw}' (use 'employer' or leave blank)")
        if err:
            errors.append(f"row {n}: {'; '.join(err)}")
        elif acct:
            rows.append({"n": n, "date": dte, "account_id": acct["id"],
                         "account": acct_name, "type": typ, "amount": amount,
                         "to_account_id": to_id, "to_account": to_name or None,
                         "value": value, "source": source,
                         "note": (r.get("note") or "").strip() or None})
    rows.sort(key=lambda x: (x["date"], x["n"]))
    return rows, errors


def investments_import():
    """RETIRED (2026-07-07): the one-time CSV/XLSX backfill page is retired.
    Kept as a redirect so old links don't 404; the historical importer code
    below is retained (unreachable) in case it's ever needed."""
    return redirect(url_for("investments_overview"))


def _investments_import_legacy():
    db = get_db()
    if request.method == "GET":
        return render_template("investments/import.html",
                               preview=None, errors=None, result=None)
    f = request.files.get("csv")
    if not f or not f.filename:
        flash("Choose a CSV file first.", "error")
        return redirect("/investments/import")
    rows, errors = _parse_inv_csv(db, f)
    if request.form.get("mode") == "preview" or errors:
        return render_template("investments/import.html",
                               preview=rows, errors=errors, result=None)
    try:
        for r in rows:
            if r["type"] == "contribution":
                ieng.record_contribution(db, account_id=r["account_id"],
                    event_date=r["date"], amount=r["amount"], note=r["note"],
                    source=r.get("source", "you"))
            elif r["type"] == "withdrawal":
                ieng.record_withdrawal(db, account_id=r["account_id"],
                    event_date=r["date"], amount=r["amount"], note=r["note"])
            elif r["type"] == "move":
                ieng.record_lot_move(db, src_account_id=r["account_id"],
                    dst_account_id=r["to_account_id"], event_date=r["date"],
                    amount=r["amount"], note=r["note"])
            elif r["type"] == "snapshot":
                ieng.record_snapshot(db, account_id=r["account_id"],
                    event_date=r["date"], snapshot_value=r["value"],
                    note=r["note"])
            elif r["type"] in ("dividend", "interest", "fee", "gain_loss"):
                ieng.record_adjustment(db, account_id=r["account_id"],
                    event_date=r["date"], kind=r["type"],
                    amount=r["amount"], note=r["note"])
            elif r["type"] == "close":
                ieng.record_closure(db, account_id=r["account_id"],
                    event_date=r["date"], dest_account_id=r["to_account_id"],
                    note=r["note"])
                db.execute("UPDATE accounts SET closed_date=? WHERE id=?",
                           (r["date"], r["account_id"]))
    except ValueError as e:
        db.rollback()
        return render_template("investments/import.html", preview=rows,
            errors=[f"row {r['n']}: {e} — NOTHING was imported (fix + re-upload)"],
            result=None)
    db.commit()
    return render_template("investments/import.html", preview=None,
                           errors=None, result=f"{len(rows)} rows imported.")


def investments_docs():
    """In-portal explainer: what the investments tracker is, how the pieces
    fit together (incl. how it ties into the rest of the portal / transfers),
    every event type, and the one-time backfill workflow."""
    return render_template("investments/docs.html")


def register(app, helpers):
    """Bind this module's routes. Endpoint names == function names,
    exactly as in the pre-split app.py."""
    login_required = helpers["login_required"]
    global investments_overview, investments_transfer, \
        investments_transactions, investments_account, investments_ledger, \
        investments_import, investments_csv_template, api_inv_event_add, \
        api_inv_sync_trx, api_inv_account_meta, api_inv_account_add, \
        investments_docs, api_inv_event_note
    investments_overview = login_required(investments_overview)
    app.route("/investments/overview")(investments_overview)
    app.route("/investments")(investments_overview)
    investments_docs = login_required(investments_docs)
    app.route("/investments/docs")(investments_docs)
    investments_transfer = login_required(investments_transfer)
    app.route("/investments/transfer", methods=["GET","POST"])(investments_transfer)
    investments_transactions = login_required(investments_transactions)
    app.route("/investments/transactions")(investments_transactions)
    investments_account = login_required(investments_account)
    app.route("/investments/account/<int:account_id>")(investments_account)
    investments_ledger = login_required(investments_ledger)
    app.route("/investments/ledger")(investments_ledger)
    investments_import = login_required(investments_import)
    app.route("/investments/import", methods=["GET", "POST"])(investments_import)
    investments_csv_template = login_required(investments_csv_template)
    app.route("/investments/import/template")(investments_csv_template)
    api_inv_event_add = login_required(api_inv_event_add)
    app.route("/api/investments/event", methods=["POST"])(api_inv_event_add)
    api_inv_event_note = login_required(api_inv_event_note)
    app.route("/api/investments/event/<int:event_id>/note", methods=["POST"])(api_inv_event_note)
    api_inv_sync_trx = login_required(api_inv_sync_trx)
    app.route("/api/investments/sync-transfers", methods=["POST"])(api_inv_sync_trx)
    api_inv_account_meta = login_required(api_inv_account_meta)
    app.route("/api/investments/account/<int:account_id>/meta", methods=["POST"])(api_inv_account_meta)
    api_inv_account_add = login_required(api_inv_account_add)
    app.route("/api/investments/account/add", methods=["POST"])(api_inv_account_add)
