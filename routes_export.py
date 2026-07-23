"""
routes_export.py — per-report .xlsx export endpoints.

Each endpoint is a thin wrapper: read popup args → call the existing query
engine in data-only mode (render=False) → hand plain data to export_xlsx
builders → return the workbook as an attachment. READ-ONLY — no DB writes.

Sign convention (locked): true cash direction. Money in +, money out −,
applied here at export time by report:
    income              → native sign (stored + = in; refunds already −)
    expenses / taxes    → negated  (stored + = out → export −; credits
                                    stored − → export +, i.e. money back in)

Filename: YYYYMMDD_YYYYMMDD_<report>.xlsx (from, to, name). "All time" =
earliest/latest data dates on the chosen basis.

NOTE: the URL paths keep the historical /export/… prefix because the
templates link to them; renaming is a coordinated templates+routes
change for a later pass.
"""
from flask import request, send_file

from config import CURRENT_YEAR, OWNER
from db import get_db
from queries import build_trx_list, build_pivot, date_field_expr
from export_xlsx import (
    new_workbook, workbook_response_bytes, write_transactions_sheet,
    write_pivot_sheet, write_table_sheet,
)

BASIS_LABELS = {
    "trx": "trx date", "post": "post date",
    "statement": "statement date", "payment": "payment date",
}


# ─── Shared arg / lookup helpers ──────────────────────────────────────────────

def _export_range(db, *, owner, trx_type, default_basis):
    """(date_from, date_to, date_field) from popup args, with all-time
    fallback = earliest/latest data dates on the chosen basis."""
    basis = request.args.get("date_field", "").strip() or default_basis
    if basis not in BASIS_LABELS:
        basis = default_basis
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    if not (date_from and date_to):
        expr = date_field_expr(basis, alias="")
        row = db.execute(f"""
            SELECT MIN({expr}), MAX({expr}) FROM transactions
            WHERE owner=? AND trx_type=? AND status='active'
        """, (owner, trx_type)).fetchone()
        date_from = date_from or (row[0] or f"{CURRENT_YEAR}-01-01")
        date_to = date_to or (row[1] or f"{CURRENT_YEAR}-12-31")
    return date_from, date_to, basis


def _tags_map(db, ids):
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    out = {}
    for r in db.execute(f"""
        SELECT tt.trx_id, tg.name FROM transaction_tags tt
        JOIN tags tg ON tg.id = tt.tag_id
        WHERE tt.trx_id IN ({ph}) ORDER BY tg.name
    """, list(ids)):
        out.setdefault(r["trx_id"], []).append(r["name"])
    return {k: ", ".join(v) for k, v in out.items()}


def _links_map(db, ids):
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    out = {}
    for r in db.execute(f"""
        SELECT a_id, b_id FROM transaction_links
        WHERE a_id IN ({ph}) OR b_id IN ({ph})
    """, list(ids) + list(ids)):
        out.setdefault(r["a_id"], []).append(r["b_id"])
        out.setdefault(r["b_id"], []).append(r["a_id"])
    return {k: ", ".join(f"#{i}" for i in sorted(set(v))) for k, v in out.items()}


def _cash_rows(db, rows, sign):
    """DB rows → export dicts with signed amounts + tags + parent/linked."""
    ids = [r["id"] for r in rows]
    tags = _tags_map(db, ids)
    links = _links_map(db, ids)
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "trx_date": r["trx_date"], "post_date": r["post_date"],
            "statement_date": r["statement_date"],
            "payment_date": r["payment_date"],
            "vendor": r["vendor"] or r["raw_description"],
            "l1": r["l1_category"], "l2": r["l2_category"],
            "amount": round(sign * (r["amount"] or 0), 2),
            "tags": tags.get(r["id"], ""),
            "parent_id": f"#{r['parent_id']}" if r["parent_id"] else "",
            "linked": links.get(r["id"], ""),
            "note": r["note"], "status": r["status"],
        })
    return out


CASH_TRX_COLUMNS = [
    {"header": "Trx #", "key": "id", "width": 8},
    {"header": "Trx date", "key": "trx_date", "width": 11},
    {"header": "Post date", "key": "post_date", "width": 11},
    {"header": "Statement date", "key": "statement_date", "width": 14},
    {"header": "Payment date", "key": "payment_date", "width": 13},
    {"header": "Vendor", "key": "vendor"},
    {"header": "L1", "key": "l1"},
    {"header": "L2", "key": "l2"},
    {"header": "Amount", "key": "amount", "money": True, "width": 12},
    {"header": "Tags", "key": "tags"},
    {"header": "Parent trx #", "key": "parent_id", "width": 11},
    {"header": "Linked trx #", "key": "linked", "width": 11},
    {"header": "Note", "key": "note"},
    {"header": "Status", "key": "status", "width": 9},
]


def _attachment(wb, date_from, date_to, report_name):
    fname = (f"{date_from.replace('-', '')}_{date_to.replace('-', '')}"
             f"_{report_name}.xlsx")
    return send_file(
        workbook_response_bytes(wb),
        mimetype=("application/vnd.openxmlformats-officedocument"
                  ".spreadsheetml.sheet"),
        as_attachment=True, download_name=fname,
    )


def _cash_trx_export(*, report_name, title, owner, trx_type, sign,
                     default_basis, payment_cash_basis=False,
                     extra_where="", extra_params=(),
                     exclude_l1_by_default=None,
                     multiselect=True, has_link_col=True,
                     columns=CASH_TRX_COLUMNS):
    """Shared body for every cash transactions export."""
    db = get_db()
    date_from, date_to, basis = _export_range(
        db, owner=owner, trx_type=trx_type, default_basis=default_basis)
    ctx = build_trx_list(db, request,
        owner=owner, trx_type=trx_type,
        template=None, year_default=CURRENT_YEAR,
        payment_cash_basis=payment_cash_basis,
        extra_where=extra_where, extra_params=extra_params,
        exclude_l1_by_default=exclude_l1_by_default,
        multiselect=multiselect,
        has_link_col=has_link_col,
        cat_types=False,
        date_from=date_from, date_to=date_to, date_field=basis,
        render=False,
    )
    rows = _cash_rows(db, ctx["rows"], sign)
    wb = new_workbook()
    write_transactions_sheet(wb, "Transactions",
        title=title,
        period_label=f"{date_from} → {date_to}",
        basis_label=BASIS_LABELS[basis],
        columns=columns, rows=rows, total_key="amount")
    return _attachment(wb, date_from, date_to, report_name)


# ─── Overview (pivot) export helpers ─────────────────────────────────────────

def _level_args():
    """Popup L1/L2 election + include-$0 checkbox."""
    level = request.args.get("level", "l1")
    if level not in ("l1", "l2"):
        level = "l1"
    include_zero = request.args.get("include_zero") == "1"
    return level, include_zero


def _nonzero(values):
    return any(round(v or 0, 2) != 0 for v in values)


def _overview_rows(db, ctx, *, sign, level, include_zero, cat_l1_type,
                   cat_l1_extra="", l2_fallback="Uncategorized",
                   exclude_l1s=()):
    """Pivot ctx (render=False) → write_pivot_sheet data_rows.

    L1 mode: one row per canonical L1 (ctx['pivot'] carries them all).
    L2 mode: one row per (L1, L2) — canonical pairs from the categories
    table, plus any observed non-canonical L2 buckets (e.g. the NULL-L2
    fallback label), sourced from ctx['l2_monthly_flat'].
    include_zero=False drops zero-total rows (and, in L2 mode, whole L1
    groups with no surviving rows).
    """
    month_keys = ctx["month_keys"]
    l1_order = [l1 for l1 in ctx["pivot"] if l1 not in exclude_l1s]

    if level == "l1":
        rows = []
        for l1 in l1_order:
            vals = {mk: sign * (ctx["pivot"][l1].get(mk, 0) or 0)
                    for mk in month_keys}
            if include_zero or _nonzero(vals.values()):
                rows.append({"labels": [l1], "values": vals})
        return ["L1"], rows

    # L2 mode — canonical pairs + observed extras from the flat monthly map.
    canonical = {}
    for r in db.execute(
            f"SELECT l1, l2 FROM categories WHERE trx_type=?{cat_l1_extra} "
            f"ORDER BY l1, l2", (cat_l1_type,)):
        if r["l1"] not in exclude_l1s and r["l2"]:
            canonical.setdefault(r["l1"], []).append(r["l2"])

    observed = {}   # (l1, l2) → {month: total}
    for k, v in ctx["l2_monthly_flat"].items():
        l1, l2, mk = k.split("||")
        if l1 in exclude_l1s:
            continue
        observed.setdefault((l1, l2), {})[mk] = v

    rows = []
    for l1 in l1_order:
        l2s = list(canonical.get(l1, []))
        extras = sorted({l2 for (o1, l2) in observed if o1 == l1
                         and l2 not in l2s})
        for l2 in l2s + extras:
            vals = {mk: sign * (observed.get((l1, l2), {}).get(mk, 0) or 0)
                    for mk in month_keys}
            if include_zero or _nonzero(vals.values()):
                rows.append({"labels": [l1, l2], "values": vals})
    return ["L1", "L2"], rows


def _cash_overview_export(*, report_name, title, owner, trx_type, sign,
                          default_basis, payment_cash_basis=False,
                          extra_where="", cat_l1_type, cat_l1_extra="",
                          l2_fallback="Uncategorized", exclude_l1s=(),
                          extra_sheets=None):
    """Shared body for the four L1×month cash overview exports."""
    db = get_db()
    date_from, date_to, basis = _export_range(
        db, owner=owner, trx_type=trx_type, default_basis=default_basis)
    level, include_zero = _level_args()
    ctx = build_pivot(db, request,
        owner=owner, trx_type=trx_type, year_default=CURRENT_YEAR,
        payment_cash_basis=payment_cash_basis, extra_where=extra_where,
        cat_l1_type=cat_l1_type, cat_l1_extra=cat_l1_extra,
        l2_fallback=l2_fallback, top_vendor_mode=None,
        render=False,
        date_from=date_from, date_to=date_to, date_field=basis)
    # categories-table filter reuses build_pivot's cat_l1_extra convention
    # (e.g. " AND l1 != 'Taxes'") — safe to inline, not user input.
    headers, rows = _overview_rows(db, ctx, sign=sign, level=level,
        include_zero=include_zero, cat_l1_type=cat_l1_type,
        cat_l1_extra=cat_l1_extra, l2_fallback=l2_fallback,
        exclude_l1s=exclude_l1s)
    wb = new_workbook()
    period = f"{date_from} → {date_to}"
    write_pivot_sheet(wb, "Overview",
        title=title, period_label=period,
        basis_label=BASIS_LABELS[basis],
        row_headers=headers, month_keys=ctx["month_keys"], data_rows=rows)
    if extra_sheets:
        extra_sheets(wb, db, ctx, date_from, date_to, basis,
                     level, include_zero, period)
    return _attachment(wb, date_from, date_to, report_name)


def _income_extra_sheets(wb, db, ctx, date_from, date_to, basis,
                         level, include_zero, period):
    """Income overview's two extra sheets: Taxes + Net."""
    month_keys = ctx["month_keys"]
    expr = date_field_expr(basis, alias="")
    fmt = "%Y-%m"

    # ── Taxes sheet (contra-income; native negative signs) ────────────────
    tax_flat = {}
    for r in db.execute(f"""
        SELECT COALESCE(l2_category,'Uncategorized') AS l2,
               strftime('{fmt}', {expr}) AS m, SUM(amount) AS total
        FROM transactions
        WHERE owner='{OWNER}' AND trx_type='income' AND status='active'
          AND l1_category='Taxes' AND {expr} BETWEEN ? AND ?
        GROUP BY l2, m
    """, (date_from, date_to)):
        tax_flat.setdefault(r["l2"], {})[r["m"]] = r["total"]

    if level == "l1":
        vals = {mk: sum(tax_flat.get(l2, {}).get(mk, 0) or 0
                        for l2 in tax_flat) for mk in month_keys}
        tax_rows = ([{"labels": ["Taxes"], "values": vals}]
                    if include_zero or _nonzero(vals.values()) else [])
        tax_headers = ["L1"]
    else:
        canonical = [r["l2"] for r in db.execute("""
            SELECT DISTINCT l2 FROM categories
            WHERE trx_type='income' AND l1='Taxes' AND l2 IS NOT NULL
            ORDER BY l2""")]
        l2s = canonical + sorted(set(tax_flat) - set(canonical))
        tax_rows = []
        for l2 in l2s:
            vals = {mk: tax_flat.get(l2, {}).get(mk, 0) or 0
                    for mk in month_keys}
            if include_zero or _nonzero(vals.values()):
                tax_rows.append({"labels": ["Taxes", l2], "values": vals})
        tax_headers = ["L1", "L2"]
    write_pivot_sheet(wb, "Taxes",
        title="Income — Taxes (contra-income)", period_label=period,
        basis_label=BASIS_LABELS[basis],
        row_headers=tax_headers, month_keys=month_keys, data_rows=tax_rows)

    # ── Net sheet: Total income (incl. Taxes contra → after-tax cash in),
    #    Total expenses (−), Net = Σ. True-cash-direction throughout. ──────
    def _monthly(trx_type, sign):
        out = {mk: 0.0 for mk in month_keys}
        for r in db.execute(f"""
            SELECT strftime('{fmt}', {expr}) AS m, SUM(amount) AS total
            FROM transactions
            WHERE owner='{OWNER}' AND trx_type=? AND status='active'
              AND {expr} BETWEEN ? AND ?
            GROUP BY m
        """, (trx_type, date_from, date_to)):
            if r["m"] in out:
                out[r["m"]] = sign * (r["total"] or 0)
        return out

    inc = _monthly("income", +1)     # includes Taxes rows (stored negative)
    exp = _monthly("expense", -1)    # money out → negative
    net = {mk: inc[mk] + exp[mk] for mk in month_keys}
    write_pivot_sheet(wb, "Net",
        title="Income — Net", period_label=period,
        basis_label=BASIS_LABELS[basis],
        row_headers=[""], month_keys=month_keys,
        data_rows=[
            {"labels": ["Total income"], "values": inc},
            {"labels": ["Total expenses"], "values": exp},
            {"labels": ["Net"], "values": net, "bold": True},
        ],
        include_total_row=False)


# ─── The endpoints ────────────────────────────────────────────────────────────

def export_income_overview():
    return _cash_overview_export(
        report_name="income_overview",
        title="Income — Overview (gross, excl. Taxes)",
        owner=OWNER, trx_type="income", sign=+1, default_basis="trx",
        extra_where="(l1_category IS NULL OR l1_category != 'Taxes')",
        cat_l1_type="income", cat_l1_extra=" AND l1 != 'Taxes'",
        extra_sheets=_income_extra_sheets)


def export_expenses_overview():
    return _cash_overview_export(
        report_name="expenses_overview",
        title="Expenses — Overview",
        owner=OWNER, trx_type="expense", sign=-1, default_basis="trx",
        cat_l1_type="expense")


def export_income_transactions():
    return _cash_trx_export(
        report_name="income_transactions",
        title="Income — Transactions",
        owner=OWNER, trx_type="income", sign=+1, default_basis="trx")


def export_expenses_transactions():
    return _cash_trx_export(
        report_name="expenses_transactions",
        title="Expenses — Transactions",
        owner=OWNER, trx_type="expense", sign=-1, default_basis="trx")


# ─── Investments exports (lot/event engine) ──────────────────────────────────

# Spec: account-POV signs on the ledger export. gain_loss keeps its native
# (already-signed) amount; snapshot/closure have no flow amount.
_EVENT_EXPORT_SIGN = {
    "contribution": +1, "lot_move_in": +1, "dividend": +1, "interest": +1,
    "withdrawal": -1, "lot_move_out": -1, "fee": -1,
}


def export_investments_transactions():
    """Ledger export: date range on event_date + accounts/kinds multiselects
    (mirrors the on-screen Transactions filters). No date-basis selector."""
    db = get_db()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    if not (date_from and date_to):
        row = db.execute(
            "SELECT MIN(event_date), MAX(event_date) FROM investment_events"
        ).fetchone()
        date_from = date_from or (row[0] or f"{CURRENT_YEAR}-01-01")
        date_to = date_to or (row[1] or f"{CURRENT_YEAR}-12-31")

    where = ["e.event_date BETWEEN ? AND ?"]
    params = [date_from, date_to]
    sel_accts = [a for a in request.args.getlist("accts") if a]
    if sel_accts:
        where.append("e.account_id IN (%s)" % ",".join("?" * len(sel_accts)))
        params += sel_accts
    sel_kinds = [k for k in request.args.getlist("kinds") if k]
    if sel_kinds:
        where.append("e.kind IN (%s)" % ",".join("?" * len(sel_kinds)))
        params += sel_kinds

    events = db.execute(f"""
        SELECT e.*, a.name AS acct_name, l.source AS lot_source
        FROM investment_events e
        JOIN accounts a ON a.id = e.account_id
        LEFT JOIN investment_lots l ON l.id = e.lot_id
        WHERE {' AND '.join(where)}
        ORDER BY e.event_date DESC, e.id DESC
    """, params).fetchall()

    rows = []
    for e in events:
        sign = _EVENT_EXPORT_SIGN.get(e["kind"])
        if e["amount"] is None or e["kind"] in ("snapshot", "closure"):
            amount = None
        elif sign is None:          # gain_loss: native signed amount
            amount = round(float(e["amount"]), 2)
        else:
            amount = round(sign * abs(float(e["amount"])), 2)
        rows.append({
            "event_date": e["event_date"], "account": e["acct_name"],
            "kind": e["kind"], "amount": amount,
            "snapshot_value": e["snapshot_value"],
            "source": e["lot_source"] or "",
            "linked": f"#{e['linked_trx_id']}" if e["linked_trx_id"] else "",
            "note": e["note"],
        })

    wb = new_workbook()
    write_transactions_sheet(wb, "Transactions",
        title="Investments — Transactions (event ledger)",
        period_label=f"{date_from} → {date_to}",
        basis_label="event date",
        columns=[
            {"header": "Event date", "key": "event_date", "width": 11},
            {"header": "Account", "key": "account"},
            {"header": "Type", "key": "kind", "width": 13},
            {"header": "Amount", "key": "amount", "money": True, "width": 13},
            {"header": "Snapshot value", "key": "snapshot_value",
             "money": True, "width": 14},
            {"header": "Source", "key": "source", "width": 10},
            {"header": "Linked trx #", "key": "linked", "width": 11},
            {"header": "Note", "key": "note"},
        ],
        rows=rows, total_key=None)
    return _attachment(wb, date_from, date_to, "investments_transactions")


def export_investments_overview():
    """As-of-today snapshot of the portfolio (no date range — spec §4).
    Sheets: Portfolio total / By group / Employer contributions.
    Group-level XIRR is computed from the group's aggregated flows (the
    spec's lean); group TWR is blank (not well-defined across accounts
    with independent snapshot calendars)."""
    from datetime import date as _date
    import investments_engine as ieng
    import investments_returns as iret
    db = get_db()

    accts = db.execute(
        "SELECT * FROM accounts WHERE type='investment' ORDER BY name"
    ).fetchall()
    per_acct = []
    for a in accts:
        value = ieng.account_value(db, a["id"])
        principal = ieng.account_principal(db, a["id"])
        n_lots = db.execute("""SELECT COUNT(*) FROM investment_lots
            WHERE current_account_id=? AND closed_at IS NULL""",
            (a["id"],)).fetchone()[0]
        has_events = db.execute(
            "SELECT 1 FROM investment_events WHERE account_id=? LIMIT 1",
            (a["id"],)).fetchone()
        if value > 0.005 or principal > 0.005 or n_lots or has_events:
            per_acct.append({"acct": a, "value": value,
                             "principal": principal, "n_lots": n_lots})

    total_value = sum(r["value"] for r in per_acct)
    total_principal = sum(r["principal"] for r in per_acct)
    today = _date.today().isoformat()
    period = f"As of {today}"

    wb = new_workbook()
    write_table_sheet(wb, "Portfolio total",
        title="Investments — Portfolio total", period_label=period,
        columns=[
            {"header": "Value", "key": "value", "money": True, "width": 15},
            {"header": "Principal", "key": "principal", "money": True,
             "width": 15},
            {"header": "Gain", "key": "gain", "money": True, "width": 15},
            {"header": "XIRR", "key": "xirr", "pct": True, "width": 10},
        ],
        rows=[{"value": round(total_value, 2),
               "principal": round(total_principal, 2),
               "gain": round(total_value - total_principal, 2),
               "xirr": iret.global_xirr(db, total_value), "bold": True}])

    # ── By group: subtotals only + Total row ──────────────────────────────
    group_rows = []
    for grp in ("Retirement", "General Savings", "Alternatives"):
        members = [r for r in per_acct
                   if (r["acct"]["inv_group"] or "Alternatives") == grp]
        if not members:
            continue
        g_value = sum(r["value"] for r in members)
        g_principal = sum(r["principal"] for r in members)
        flows = []
        for r in members:
            flows.extend(iret.account_flows(db, r["acct"]["id"]))
        if g_value > 1e-9:
            flows.append((today, g_value))
        group_rows.append({
            "group": grp, "value": round(g_value, 2),
            "principal": round(g_principal, 2),
            "gain": round(g_value - g_principal, 2),
            "xirr": iret.xirr(flows), "twr": None,
            "lots": sum(r["n_lots"] for r in members)})
    group_rows.append({
        "group": "Total", "value": round(total_value, 2),
        "principal": round(total_principal, 2),
        "gain": round(total_value - total_principal, 2),
        "xirr": iret.global_xirr(db, total_value), "twr": None,
        "lots": sum(r["n_lots"] for r in per_acct), "bold": True})
    write_table_sheet(wb, "By group",
        title="Investments — By group", period_label=period,
        columns=[
            {"header": "Group", "key": "group", "width": 18},
            {"header": "Value", "key": "value", "money": True, "width": 15},
            {"header": "Principal", "key": "principal", "money": True,
             "width": 15},
            {"header": "Gain", "key": "gain", "money": True, "width": 15},
            {"header": "XIRR", "key": "xirr", "pct": True, "width": 10},
            {"header": "TWR/yr", "key": "twr", "pct": True, "width": 10},
            {"header": "Lots", "key": "lots", "width": 8},
        ],
        rows=group_rows)

    # ── Employer contributions ────────────────────────────────────────────
    emp = iret.employer_summary(db)
    emp_rows = [{"year": r["year"], "orig": r["original_account"],
                 "cur": r["current_account"], "given": round(r["given"], 2),
                 "value": round(r["value"], 2), "gain": round(r["gain"], 2)}
                for r in emp["rows"]]
    emp_rows.append({"year": "Total", "orig": "", "cur": "",
                     "given": round(emp["given"], 2),
                     "value": round(emp["value"], 2),
                     "gain": round(emp["gain"], 2), "bold": True})
    write_table_sheet(wb, "Employer contributions",
        title="Investments — Employer contributions", period_label=period,
        columns=[
            {"header": "Year", "key": "year", "width": 8},
            {"header": "Original account", "key": "orig"},
            {"header": "Current account", "key": "cur"},
            {"header": "Given", "key": "given", "money": True, "width": 14},
            {"header": "Now worth", "key": "value", "money": True,
             "width": 14},
            {"header": "Gain", "key": "gain", "money": True, "width": 14},
        ],
        rows=emp_rows)

    d0 = db.execute("SELECT MIN(event_date) FROM investment_events"
                    ).fetchone()[0] or today
    return _attachment(wb, d0, today, "investments_overview")


def register(app, helpers):
    """Bind export routes (same pattern as the other route modules)."""
    login_required = helpers["login_required"]
    global export_income_transactions, export_expenses_transactions, \
        export_income_overview, export_expenses_overview, \
        export_investments_overview, export_investments_transactions
    export_investments_overview = login_required(export_investments_overview)
    app.route("/export/investments-overview.xlsx")(export_investments_overview)
    export_investments_transactions = login_required(export_investments_transactions)
    app.route("/export/investments-transactions.xlsx")(export_investments_transactions)
    export_income_overview = login_required(export_income_overview)
    app.route("/export/income-overview.xlsx")(export_income_overview)
    export_expenses_overview = login_required(export_expenses_overview)
    app.route("/export/expenses-overview.xlsx")(export_expenses_overview)
    export_income_transactions = login_required(export_income_transactions)
    app.route("/export/income-transactions.xlsx")(export_income_transactions)
    export_expenses_transactions = login_required(export_expenses_transactions)
    app.route("/export/expenses-transactions.xlsx")(export_expenses_transactions)
