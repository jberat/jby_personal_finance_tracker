"""
routes_lists.py — list/overview/dashboard routes for Personal Financial
Tracker: the dashboard, expense list/overview/by-vendor pages, and
income list/overview pages.

No blueprints: register(app, helpers) binds every view under its original
function name, so endpoint names, url_for(...) and base.html `ep ==` checks
are unchanged.
"""
from flask import request, redirect, url_for, render_template
from config import CURRENT_YEAR, OWNER
from db import get_db
from queries import build_trx_list, build_pivot

# ─── Dashboard ────────────────────────────────────────────────────────────────

def dashboard():
    db = get_db()
    year = request.args.get("year", CURRENT_YEAR)

    # YTD expense total
    ytd_expense = db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE owner=? AND trx_type='expense' AND status='active'
        AND strftime('%Y', trx_date) = ?
    """, (OWNER, year)).fetchone()[0]

    # YTD income (gross). EXCLUDES Taxes L1 (which lives in the income tree
    # as contra-income post-Chunk-1). Shows the gross number, matching the
    # Personal P&L's "Income (gross)" line.
    ytd_income = db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE owner=? AND trx_type='income' AND status='active'
          AND strftime('%Y', trx_date) = ?
          AND (l1_category IS NULL OR l1_category != 'Taxes')
    """, (OWNER, year)).fetchone()[0]

    # Pending review count
    pending = db.execute("""
        SELECT COUNT(*) FROM staging
         WHERE status IN ('pending','duplicate') AND owner=?
    """, (OWNER,)).fetchone()[0]

    # Investments — TOTAL PORTFOLIO VALUE from the lot engine (sum of every
    # open lot's current value), which is the real source of truth. The old
    # opening-balance + transfers math is retired. Plus net cash flow for the
    # selected year below.
    inv_value = db.execute(
        "SELECT COALESCE(SUM(current_value), 0) FROM investment_lots WHERE closed_at IS NULL"
    ).fetchone()[0] or 0
    # Lifetime gain + money-weighted return for the card sub-line.
    import investments_returns as _iret
    _gf = _iret.global_flows(db)
    _net_in = sum(-a for _, a in _gf if a < 0) - sum(a for _, a in _gf if a > 0)
    inv_gain = inv_value - _net_in
    inv_xirr = _iret.global_xirr(db, inv_value)

    # Net cash flow into investments for the SELECTED year (negated)
    inv_year_transfers = db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE trx_type='transfer' AND status='active'
          AND l1_category IN ('Retirement','General Savings','Alternatives')
          AND l2_category IS NOT NULL
          AND strftime('%Y', trx_date) = ?
    """, (year,)).fetchone()[0] or 0
    inv_year_net = -inv_year_transfers

    # Recent approved transactions (last 10)
    recent = db.execute("""
        SELECT t.*, a.name as account_name
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.owner=? AND t.status='active'
        ORDER BY t.trx_date DESC, t.id DESC
        LIMIT 10
    """, (OWNER,)).fetchall()

    # Top 5 expense categories YTD
    top_cats = db.execute("""
        SELECT l1_category, SUM(amount) as total
        FROM transactions
        WHERE owner=? AND trx_type='expense' AND status='active'
        AND strftime('%Y', trx_date) = ?
        AND l1_category IS NOT NULL
        GROUP BY l1_category
        ORDER BY total DESC
        LIMIT 5
    """, (OWNER, year)).fetchall()

    available_years = db.execute("""
        SELECT DISTINCT strftime('%Y', trx_date) as yr
        FROM transactions WHERE owner=? AND status='active'
        ORDER BY yr DESC
    """, (OWNER,)).fetchall()

    # ── Income Statement card ────────────────────────────────────────────
    # Income (gross) = positive income trxs where l1 != 'Taxes' (Taxes
    # lives in income tree as contra-income, would falsely reduce gross
    # if grouped with the wage/salary income lines).
    is_income = db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
         WHERE owner=? AND status='active' AND trx_type='income'
           AND strftime('%Y', trx_date) = ?
           AND (l1_category IS NULL OR l1_category != 'Taxes')
    """, (OWNER, year)).fetchone()[0] or 0.0

    # Taxes paid = ABS of sum of contra-income Tax rows (stored as negative).
    # Break down into Federal / State for display.
    is_tax_total = db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
         WHERE owner=? AND status='active' AND trx_type='income'
           AND l1_category='Taxes' AND strftime('%Y', trx_date) = ?
    """, (OWNER, year)).fetchone()[0] or 0.0
    is_tax_paid = -is_tax_total  # display as positive

    is_tax_fed = -(db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
         WHERE owner=? AND status='active' AND trx_type='income'
           AND l1_category='Taxes' AND strftime('%Y', trx_date) = ?
           AND l2_category LIKE 'Fed%'
    """, (OWNER, year)).fetchone()[0] or 0.0)

    is_tax_state = -(db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
         WHERE owner=? AND status='active' AND trx_type='income'
           AND l1_category='Taxes' AND strftime('%Y', trx_date) = ?
           AND l2_category LIKE 'State%'
    """, (OWNER, year)).fetchone()[0] or 0.0)

    # Investments (out-of-pocket) — only count transfers from non-investment
    # accounts to investment accounts. Inter-investment moves (e.g. Trad IRA
    # → Roth IRA) are NOT counted (no new capital deployed). Standalone
    # employer match (positive amount on cash side) is also excluded since
    # it's free money, not out-of-pocket.
    is_invest_total = db.execute("""
        SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions t
         WHERE t.owner=? AND t.status='active' AND t.trx_type='transfer'
           AND t.amount < 0
           AND t.l1_category IN ('Retirement', 'General Savings', 'Alternatives')
           AND strftime('%Y', t.trx_date) = ?
           AND t.account_id IN (SELECT id FROM accounts WHERE type != 'investment')
    """, (OWNER, year)).fetchone()[0] or 0.0

    is_invest_breakdown = []
    for l1 in ('Retirement', 'General Savings', 'Alternatives'):
        v = db.execute("""
            SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions t
             WHERE t.owner=? AND t.status='active' AND t.trx_type='transfer'
               AND t.amount < 0 AND t.l1_category=?
               AND strftime('%Y', t.trx_date) = ?
               AND t.account_id IN (SELECT id FROM accounts WHERE type != 'investment')
        """, (OWNER, l1, year)).fetchone()[0] or 0.0
        if abs(v) > 0.005:
            is_invest_breakdown.append((l1, v))

    # Expenses = sum of expense trxs (already excludes Taxes since those moved to income tree)
    is_expense = db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
         WHERE owner=? AND status='active' AND trx_type='expense'
           AND strftime('%Y', trx_date) = ?
    """, (OWNER, year)).fetchone()[0] or 0.0

    # Remaining = income − taxes − investments − expenses
    is_remaining = is_income - is_tax_paid - is_invest_total - is_expense

    return render_template("dashboard.html",
        year=year,
        ytd_expense=ytd_expense,
        ytd_income=ytd_income,
        pending=pending,
        recent=recent,
        top_cats=top_cats,
        inv_value=inv_value,
        inv_year_net=inv_year_net,
        inv_gain=inv_gain,
        inv_xirr=inv_xirr,
        # Income Statement card values
        is_income=is_income, is_tax_paid=is_tax_paid,
        is_tax_fed=is_tax_fed, is_tax_state=is_tax_state,
        is_invest_total=is_invest_total, is_invest_breakdown=is_invest_breakdown,
        is_expense=is_expense, is_remaining=is_remaining,
        available_years=[r["yr"] for r in available_years] or [year],
    )

# ─── Expenses ─────────────────────────────────────────────────────────────────

def expenses():
    return redirect(url_for("expenses_transactions"))

def expenses_overview():
    # Taxes were moved out of the expense tree (Chunk 1, payroll-recon
    # foundation). They now live in income/Taxes as contra-income, so the
    # main pivot is naturally tax-free. Income overview owns the Taxes
    # table now.
    return build_pivot(get_db(), request,
        owner=OWNER, trx_type="expense",
        template="expenses/overview.html",
        year_default=CURRENT_YEAR,
        cat_l1_type="expense",
        top_vendor_mode="sum",   # rank top vendor by dollars, not count
        years_owner=OWNER,
    )

def expenses_transactions():
    # NOTE(refactor): year default is a hardcoded "2026" (other list routes
    # use CURRENT_YEAR) — preserved as-is.
    return build_trx_list(get_db(), request,
        owner=OWNER, trx_type="expense",
        template="expenses/transactions.html",
        year_default="2026",
        acct_filter=True,
        no_receipt_filter=True,
        q_matches_tags=True,
        l1s_from=("python", "expense"),
        cat_types=["expense"],
        l2_prefix="expense:",
        accounts_sql="SELECT * FROM accounts WHERE active=1",
        years_owner=None,  # NOTE(refactor): unscoped — preserved.
    )

def expenses_by_vendor():
    db   = get_db()
    year = request.args.get("year", CURRENT_YEAR)
    l1   = request.args.get("l1", "")
    page = request.args.get("page", 1, type=int)
    per  = 50

    filters = ["trx_type='expense'", "status='active'", "owner=?",
               "strftime('%Y', trx_date)=?"]
    params  = [OWNER, year]
    if l1:
        filters.append("l1_category=?"); params.append(l1)

    where = " AND ".join(filters)

    total_rows = db.execute(
        f"SELECT COUNT(DISTINCT COALESCE(vendor, raw_description)) FROM transactions WHERE {where}",
        params
    ).fetchone()[0]

    vendors = db.execute(f"""
        SELECT COALESCE(vendor, raw_description) as vendor_name,
               COUNT(*) as trx_count,
               SUM(amount) as total,
               MAX(trx_date) as last_date,
               l1_category
        FROM transactions
        WHERE {where}
        GROUP BY vendor_name
        ORDER BY total DESC
        LIMIT ? OFFSET ?
    """, params + [per, (page - 1) * per]).fetchall()

    l1s = db.execute(
        "SELECT DISTINCT l1 FROM categories WHERE trx_type='expense' ORDER BY l1"
    ).fetchall()

    available_years = db.execute("""
        SELECT DISTINCT strftime('%Y', trx_date) as yr
        FROM transactions WHERE status='active' ORDER BY yr DESC
    """).fetchall()

    return render_template("expenses/by_vendor.html",
        vendors=vendors, year=year, l1=l1,
        page=page, per=per, total_rows=total_rows,
        l1s=[r["l1"] for r in l1s],
        available_years=[r["yr"] for r in available_years] or [year],
    )

# ─── Income ───────────────────────────────────────────────────────────────────

def income():
    return redirect(url_for("income_transactions"))

def income_overview():
    db   = get_db()
    year = request.args.get("year", CURRENT_YEAR)

    # Taxes live in the income tree as contra-income (Chunk 1, payroll-recon
    # foundation). Main pivot + stat cards show GROSS income (excl. Taxes);
    # a dedicated Taxes table shows the contra-income breakdown; a third
    # Net Income table shows gross + taxes summed per month.
    #
    # The gross pivot + stat cards are the shared overview pattern → built by
    # build_pivot (render=False so we can extend the context). The Taxes and
    # Net tables are unique to this page and stay inline below.
    ctx = build_pivot(db, request,
        owner=OWNER, trx_type="income",
        year_default=CURRENT_YEAR,
        extra_where="(l1_category IS NULL OR l1_category != 'Taxes')",
        cat_l1_type="income", cat_l1_extra=" AND l1 != 'Taxes'",
        top_vendor_mode=None,    # income overview has no top-vendor card
        years_owner=OWNER,
        render=False,
    )

    INC_TAX = ("owner=? AND trx_type='income' AND status='active' "
               "AND l1_category = 'Taxes' AND strftime('%Y', trx_date)=?")

    # ── Taxes pivot (contra-income) — rows are L2s under Taxes L1 ───────────
    tax_rows = db.execute(f"""
        SELECT strftime('%m', trx_date) as month,
               COALESCE(l2_category, 'Uncategorized') AS l2,
               SUM(amount) AS total
        FROM transactions WHERE {INC_TAX}
        GROUP BY month, l2 ORDER BY month, l2
    """, (OWNER, year)).fetchall()
    tax_l2_totals = db.execute(f"""
        SELECT COALESCE(l2_category, 'Uncategorized') AS l2,
               SUM(amount) AS total, COUNT(*) AS cnt
        FROM transactions WHERE {INC_TAX}
        GROUP BY l2 ORDER BY total
    """, (OWNER, year)).fetchall()
    tax_l2_canonical = db.execute("""
        SELECT DISTINCT l2 FROM categories
         WHERE trx_type='income' AND l1='Taxes' AND l2 IS NOT NULL ORDER BY l2
    """).fetchall()
    tax_l2_list = [r["l2"] for r in tax_l2_canonical]
    for r in tax_l2_totals:
        if r["l2"] not in tax_l2_list:
            tax_l2_list.append(r["l2"])

    tax_pivot = {l2: {str(m).zfill(2): 0.0 for m in range(1, 13)} for l2 in tax_l2_list}
    for r in tax_rows:
        if r["l2"] in tax_pivot:
            tax_pivot[r["l2"]][r["month"]] = r["total"]
    tax_totals = {l2: sum(tax_pivot[l2].values()) for l2 in tax_l2_list}
    tax_cnt    = {r["l2"]: r["cnt"] for r in tax_l2_totals}

    # ── Net Income per month — gross totals + tax totals (signs preserved) ──
    # Tax amounts are stored as negative (contra-income); add them directly
    # and you get net. e.g. gross $5000 + (-$2000 taxes) = $3000 net.
    # NOTE(refactor): iterates ctx["pivot"] keys — identical set + order to
    # the pre-hiding l1_list the original used (pivot keeps ALL canonical
    # L1s; only ctx["l1_list"]/ctx["l2_map"] get the zero-row hiding pass).
    net_pivot = {str(m).zfill(2): 0.0 for m in range(1, 13)}
    for l1 in ctx["pivot"]:
        for mm, v in ctx["pivot"][l1].items():
            net_pivot[mm] += v
    for l2 in tax_l2_list:
        for mm, v in tax_pivot[l2].items():
            net_pivot[mm] += v
    net_total = sum(net_pivot.values())

    return render_template("income/overview.html",
        **ctx,
        # Taxes block
        tax_l2_list=tax_l2_list, tax_pivot=tax_pivot,
        tax_totals=tax_totals, tax_cnt=tax_cnt,
        # Net Income summary
        net_pivot=net_pivot, net_total=net_total,
    )

def income_transactions():
    # Owner scoping (2026-07-03 fix): without it, other owners' income rows
    # leaked into — and summed into — this list.
    return build_trx_list(get_db(), request,
        owner=OWNER, trx_type="income",
        template="income/transactions.html",
        year_default=CURRENT_YEAR,
        acct_filter=True,
        l1s_from=("sql", "income"),
        cat_types=None,  # NOTE(refactor): cat_map spans ALL types here (vs. expense-only on the expenses page) — preserved.
        l2_prefix="income:",
        accounts_sql="SELECT * FROM accounts WHERE active=1 ORDER BY name",
        years_owner=OWNER,
    )


def register(app, helpers):
    """Bind this module's routes. Endpoint names == function names,
    exactly as in the pre-split app.py."""
    login_required = helpers["login_required"]
    global dashboard, expenses, expenses_overview, expenses_transactions, \
        expenses_by_vendor, income, income_overview, income_transactions
    dashboard = login_required(dashboard)
    app.route("/")(dashboard)
    expenses = login_required(expenses)
    app.route("/expenses")(expenses)
    expenses_overview = login_required(expenses_overview)
    app.route("/expenses/overview")(expenses_overview)
    expenses_transactions = login_required(expenses_transactions)
    app.route("/expenses/transactions")(expenses_transactions)
    expenses_by_vendor = login_required(expenses_by_vendor)
    app.route("/expenses/by-vendor")(expenses_by_vendor)
    income = login_required(income)
    app.route("/income")(income)
    income_overview = login_required(income_overview)
    app.route("/income/overview")(income_overview)
    income_transactions = login_required(income_transactions)
    app.route("/income/transactions")(income_transactions)
