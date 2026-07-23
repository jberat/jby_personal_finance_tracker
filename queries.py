"""
queries.py — the shared list/pivot query engine.

build_trx_list powers every transaction-list page (expenses, income, and
any pinned-category variants) and build_pivot powers the overview/pivot
pages (L1 × month, with an L2 × month shape available via by_l2=True).
Each route is a thin call into these builders with its exact nuances
passed explicitly.

Naming note: some helper names here are historical. They simply mean
"payment-date cash basis" and are kept because route modules and
exports call them by name.

Every quirk of the original routes (legacy singular l1/l2 fallbacks,
per-route available_years scoping, the unscoped year list on expenses,
cash-basis COALESCE date expressions, etc.) is preserved deliberately —
do not "fix" anything here without a snapshot-diff proof.
"""
from flask import render_template


# ─── Cash-basis (payment-date) date filter ────────────────────────────────────

QTR_RANGES = {
    "Q1": ("01", "03"), "Q2": ("04", "06"),
    "Q3": ("07", "09"), "Q4": ("10", "12"),
}


def period_date_filter(year, qtr, date_col="COALESCE(t.payment_date, t.trx_date)"):
    """Return (sql_fragment, params) for year+optional quarter filter on a payment-date column."""
    if qtr and qtr in QTR_RANGES:
        m0, m1 = QTR_RANGES[qtr]
        return (f"{date_col} BETWEEN ? AND ?",
                [f"{year}-{m0}-01", f"{year}-{m1}-31"])
    return (f"strftime('%Y', {date_col})=?", [year])


# ─── Export date-basis support (2026-07-10, additive — see docs/handbook.html §13) ─
# The four selectable date bases. Non-trx bases COALESCE to trx_date, matching
# the app's existing cash-basis convention (payment_date falls back to trx_date
# for non-CC accounts; post/statement likewise for rows that lack them).

def date_field_expr(date_field, alias="t."):
    """SQL expression for a date-basis choice: trx|post|statement|payment."""
    exprs = {
        "trx":       f"{alias}trx_date",
        "post":      f"COALESCE({alias}post_date, {alias}trx_date)",
        "statement": f"COALESCE({alias}statement_date, {alias}trx_date)",
        "payment":   f"COALESCE({alias}payment_date, {alias}trx_date)",
    }
    return exprs[date_field]


def month_span(date_from, date_to):
    """List of 'YYYY-MM' keys covering [date_from, date_to] inclusive."""
    y, m = int(date_from[:4]), int(date_from[5:7])
    y2, m2 = int(date_to[:4]), int(date_to[5:7])
    keys = []
    while (y, m) <= (y2, m2):
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return keys


# ─── Shared helpers ───────────────────────────────────────────────────────────

def get_cat_map(db, trx_types=None):
    """Universal CAT_MAP format: {'<trx_type>:<l1>': [l2, ...]}.

    trx_types=None  → all categories, ORDER BY trx_type, l1, l2
                      (matches the income route's construction)
    trx_types=list  → WHERE trx_type IN (...), ORDER BY l1, l2
                      (matches the expenses routes' construction)

    Key insertion order follows SQL row order — templates may serialize the
    dict (tojson), so the per-scope ORDER BY differences are preserved.
    """
    if trx_types is None:
        categories = db.execute(
            "SELECT * FROM categories ORDER BY trx_type, l1, l2"
        ).fetchall()
    else:
        placeholders = ",".join("?" * len(trx_types))
        categories = db.execute(
            f"SELECT * FROM categories WHERE trx_type IN ({placeholders}) ORDER BY l1, l2",
            list(trx_types)
        ).fetchall()
    cat_map = {}
    for c in categories:
        key = f"{c['trx_type']}:{c['l1']}"
        cat_map.setdefault(key, []).append(c["l2"])
    return cat_map


def get_available_years(db, owner=None, date_expr="trx_date"):
    """Distinct active-transaction years, DESC. Scoping quirks preserved:
    the expenses route passes owner=None (unscoped — sees all years);
    the income route passes the configured OWNER. Cash-basis callers pass
    the COALESCE(payment_date, trx_date) date expression."""
    where = "status='active'"
    if owner:
        where += f" AND owner='{owner}'"
    rows = db.execute(f"""
        SELECT DISTINCT strftime('%Y', {date_expr}) as yr
        FROM transactions WHERE {where} ORDER BY yr DESC
    """).fetchall()
    return [r["yr"] for r in rows]


def get_l2_parent_map(cat_map, prefix):
    """L2 → sorted list of parent L1s, for the L2 multi-select component.
    Same L2 name can appear under multiple L1s; tracking the set lets the
    L2 dropdown filter itself to options whose parent is in the current
    L1 selection. Only cat_map keys starting with `prefix` are considered
    (e.g. 'expense:', 'income:')."""
    l2_parent_map = {}
    for key, l2_list in cat_map.items():
        if not key.startswith(prefix):
            continue
        parent_l1 = key.split(":", 1)[1]
        for l2 in l2_list:
            if l2:
                l2_parent_map.setdefault(l2, set()).add(parent_l1)
    return {l2: sorted(parents) for l2, parents in l2_parent_map.items()}


# ─── The transaction-list builder ────────────────────────────────────────────

_LINK_SQL = """(t.id = ? OR t.id IN (
                          SELECT b_id FROM transaction_links WHERE a_id=?
                          UNION
                          SELECT a_id FROM transaction_links WHERE b_id=?))"""

_Q_SQL_PLAIN = "(t.vendor LIKE ? OR t.raw_description LIKE ? OR t.note LIKE ?)"

# Match vendor / raw description / note / any tag name (substring, so "HSA"
# finds tags like "HSA-1234" or "HSA reimbursement").
_Q_SQL_TAGS = (
    "(t.vendor LIKE ? OR t.raw_description LIKE ? OR t.note LIKE ? "
    "OR EXISTS (SELECT 1 FROM transaction_tags tt "
    "JOIN tags tg ON tg.id = tt.tag_id "
    "WHERE tt.trx_id = t.id AND tg.name LIKE ?))"
)


def build_trx_list(db, request, *, owner, trx_type, template, year_default,
                   extra_where="", extra_params=(),
                   payment_cash_basis=False,
                   link_filter=True,
                   multiselect=True,
                   acct_filter=False,
                   no_receipt_filter=False,
                   q_matches_tags=False,
                   exclude_l1_by_default=None,
                   include_total_amt=False,
                   has_link_col=True,
                   l1s_from=None,
                   cat_types=False,
                   l2_prefix=None,
                   accounts_sql=None,
                   years_owner=None,
                   date_from=None, date_to=None, date_field=None,
                   render=True, extra_ctx=None):
    """Parameterized transaction-list route body. Each list route is a thin
    call into this with its exact nuances passed explicitly.

    Flags (all preserve original per-route behavior exactly):
      payment_cash_basis        payment-date cash basis: qtr param +
                            period_date_filter for the year window,
                            month filter on COALESCE(payment_date, trx_date),
                            ORDER BY COALESCE(post_date, trx_date),
                            available_years on the payment-date expression.
                            False → trx_date everywhere.
      link_filter           ?link= group view: overrides everything except
                            status/owner/type.
      multiselect           l1s/l2s repeated params + legacy singular ?l1=/?l2=
                            fallback, plus the l1/l2 legacy context vars.
                            False → singular ?l2= filter only.
      acct_filter           ?acct= account filter + `acct` context var.
      no_receipt_filter     ?no_receipt= quick filter + context var.
      q_matches_tags        ?q= also matches tag names (expense pages).
      exclude_l1_by_default when NO l1s are selected, exclude this L1
                            (lets a pinned-category L1 hide by default).
      extra_where/params    appended to the non-link WHERE (link view ignores
                            it, matching the originals). Used for
                            pinned-L1 variants.
      include_total_amt     also compute SUM(amount) → `total_amt` context var.
      has_link_col          include the has_link EXISTS column in the row query.
      l1s_from              None → no `l1s` context var.
                            ("python", "<type>") → sorted set of L1s derived
                              from cat_map keys (the expenses construction).
                            ("sql", "<type>") → SELECT DISTINCT l1 ... ORDER BY l1.
      cat_types             False → no cat_map. None → all types. list → IN (...).
      l2_prefix             cat_map key prefix for l2_parent_map/all_l2s
                            (None → neither context var).
      accounts_sql          exact per-route accounts query (None → no var).
      years_owner           owner scope for available_years (None = unscoped,
                            which is the expenses-page quirk).
      date_from/date_to     EXPORT MODE (2026-07-10, additive): when BOTH are
                            set ('YYYY-MM-DD'), the year/qtr window is replaced
                            by `date_expr BETWEEN date_from AND date_to` on the
                            chosen date basis. Absent → exact legacy behavior.
      date_field            'trx'|'post'|'statement'|'payment' — the date basis
                            for the range filter. Default: 'payment' when
                            payment_cash_basis else 'trx' (matches on-screen basis).
      render                False → return the context dict instead of the
                            rendered template, with rows UNPAGINATED (exports
                            need every row). True (default) → legacy behavior.
    """
    year       = request.args.get("year", year_default)
    # qtr is parsed for ALL list routes (Full Year / Q1–Q4 buttons; quarters
    # window on trx_date for non-cash-basis pages).
    qtr        = request.args.get("qtr", "")
    month      = request.args.get("month", "")  # 01..12 for monthly drill-down
    # Accounts filter is a multi-select like L1/L2 (2026-07-05).
    # Legacy singular ?acct= still honored (old links / drill-downs).
    if acct_filter:
        selected_accts = [v for v in request.args.getlist("accts") if v]
        if not selected_accts and request.args.get("acct"):
            selected_accts = [request.args.get("acct")]
    else:
        selected_accts = None
    acct = (selected_accts[0] if selected_accts and len(selected_accts) == 1
            else "") if acct_filter else None
    q          = request.args.get("q", "").strip()
    link       = request.args.get("link", "").strip() if link_filter else ""
    no_receipt = request.args.get("no_receipt", "").strip() if no_receipt_filter else None
    page       = request.args.get("page", 1, type=int)
    per        = 50

    # Multi-value L1/L2 filters with backward-compat for legacy singular l1/l2
    # (overview pages link via the singular form).
    if multiselect:
        selected_l1s = [v for v in request.args.getlist("l1s") if v]
        if not selected_l1s and request.args.get("l1"):
            selected_l1s = [request.args.get("l1")]
        selected_l2s = [v for v in request.args.getlist("l2s") if v]
        if not selected_l2s and request.args.get("l2"):
            selected_l2s = [request.args.get("l2")]
        l2_single = None
    else:
        selected_l1s = selected_l2s = None
        l2_single = request.args.get("l2", "")

    base_filters = [f"t.owner='{owner}'", f"t.trx_type='{trx_type}'",
                    "t.status='active'"]

    if link:
        # Link filter overrides everything except status/owner/type — shows all
        # transactions in the link group regardless of year/category/account.
        filters = base_filters + [_LINK_SQL]
        params  = [link, link, link]
    else:
        if date_from and date_to:
            # Export mode: explicit range on the chosen date basis replaces
            # the year/qtr window entirely.
            _basis = date_field or ("payment" if payment_cash_basis else "trx")
            date_sql = f"{date_field_expr(_basis)} BETWEEN ? AND ?"
            date_params = [date_from, date_to]
        elif payment_cash_basis:
            date_sql, date_params = period_date_filter(year, qtr)
        else:
            # Same year/quarter windowing, on trx_date (2026-07-05).
            date_sql, date_params = period_date_filter(year, qtr,
                                                    date_col="t.trx_date")
        filters = base_filters[:]
        if extra_where:
            filters.append(extra_where)
        filters.append(date_sql)
        params = list(extra_params) + date_params[:]

        if multiselect:
            if selected_l1s:
                placeholders = ",".join("?" * len(selected_l1s))
                filters.append(f"t.l1_category IN ({placeholders})")
                params.extend(selected_l1s)
            elif exclude_l1_by_default:
                # Default view excludes this L1. If the user explicitly
                # filters by it they still get the rows.
                filters.append(
                    f"(t.l1_category IS NULL OR t.l1_category != '{exclude_l1_by_default}')"
                )
            if selected_l2s:
                placeholders = ",".join("?" * len(selected_l2s))
                filters.append(f"t.l2_category IN ({placeholders})")
                params.extend(selected_l2s)
        else:
            if l2_single:
                filters.append("t.l2_category=?"); params.append(l2_single)
        if month:
            # Cash basis: filter by payment_date to match the overview
            # pivot, falling back to trx_date for non-CC accounts.
            month_expr = ("COALESCE(t.payment_date, t.trx_date)"
                          if payment_cash_basis else "t.trx_date")
            filters.append(f"strftime('%m', {month_expr}) = ?")
            params.append(month)
        if acct_filter and selected_accts:
            ph = ",".join("?" * len(selected_accts))
            filters.append(f"t.account_id IN ({ph})")
            params.extend(selected_accts)
        if q:
            if q_matches_tags:
                filters.append(_Q_SQL_TAGS)
                params.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])
            else:
                filters.append(_Q_SQL_PLAIN)
                params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        if no_receipt_filter and no_receipt:
            # Exclude rows explicitly marked "no receipt needed"
            filters.append("t.receipt_path IS NULL "
                           "AND (t.no_receipt_needed IS NULL OR t.no_receipt_needed=0)")

    where = " AND ".join(filters)

    total_rows = db.execute(
        f"SELECT COUNT(*) FROM transactions t WHERE {where}", params
    ).fetchone()[0]
    total_amt = None
    if include_total_amt:
        total_amt = db.execute(
            f"SELECT COALESCE(SUM(t.amount),0) FROM transactions t WHERE {where}",
            params
        ).fetchone()[0]

    has_link_sql = (""",
               EXISTS(SELECT 1 FROM transaction_links tl
                      WHERE tl.a_id = t.id OR tl.b_id = t.id) AS has_link"""
                    if has_link_col else "")
    order_by = ("COALESCE(t.post_date, t.trx_date) DESC, t.id DESC"
                if payment_cash_basis else "t.trx_date DESC, t.id DESC")
    _page_sql, _page_params = (("LIMIT ? OFFSET ?", [per, (page - 1) * per])
                               if render else ("", []))  # exports: every row
    rows = db.execute(f"""
        SELECT t.*, a.name as account_name{has_link_sql}
        FROM transactions t JOIN accounts a ON t.account_id = a.id
        WHERE {where}
        ORDER BY {order_by}
        {_page_sql}
    """, params + _page_params).fetchall()

    # ── Per-route sidebar/context data ────────────────────────────────────
    cat_map = None
    if cat_types is not False:
        cat_map = get_cat_map(db, cat_types)

    l1s = None
    if l1s_from:
        mode, l1s_type = l1s_from
        if mode == "python":
            # expenses overview construction: sorted set of L1s from the (already
            # type-scoped) category rows — derived here from cat_map keys.
            l1s = sorted({k.split(":", 1)[1] for k in cat_map
                          if k.startswith(l1s_type + ":")})
        else:
            l1s = [r["l1"] for r in db.execute(
                "SELECT DISTINCT l1 FROM categories WHERE trx_type=? ORDER BY l1",
                (l1s_type,)
            ).fetchall()]

    accounts = db.execute(accounts_sql).fetchall() if accounts_sql else None

    date_expr = ("COALESCE(payment_date, trx_date)" if payment_cash_basis
                 else "trx_date")
    available_years = get_available_years(db, owner=years_owner,
                                          date_expr=date_expr)

    l2_parent_map = all_l2s = None
    if l2_prefix:
        l2_parent_map = get_l2_parent_map(cat_map, l2_prefix)
        all_l2s = sorted(l2_parent_map.keys())

    # ── Context — only include the vars each original route passed ───────
    ctx = dict(
        rows=rows, year=year, month=month, q=q,
        page=page, per=per, total_rows=total_rows,
        available_years=available_years or [year],
    )
    ctx["qtr"] = qtr
    if link_filter:
        ctx["link"] = link
    if acct_filter:
        ctx["acct"] = acct
        ctx["selected_accts"] = selected_accts
        ctx["acct_options"] = ([{"v": str(a["id"]), "t": a["name"]}
                                for a in accounts] if accounts else [])
        # Selected account NAMES for the filter-chip strip.
        _acct_names = {str(a["id"]): a["name"] for a in (accounts or [])}
        ctx["selected_acct_names"] = [_acct_names.get(v, v) for v in selected_accts]
    if no_receipt_filter:
        ctx["no_receipt"] = no_receipt
    if include_total_amt:
        ctx["total_amt"] = total_amt
    if l1s is not None:
        ctx["l1s"] = l1s
    if cat_map is not None:
        ctx["cat_map"] = cat_map
    if accounts is not None:
        ctx["accounts"] = accounts
    if multiselect:
        ctx.update(
            selected_l1s=selected_l1s, selected_l2s=selected_l2s,
            all_l2s=all_l2s, l2_parent_map=l2_parent_map,
            # Legacy singular vars for any unupdated template references
            l1=(selected_l1s[0] if len(selected_l1s) == 1 else ""),
            l2=(selected_l2s[0] if len(selected_l2s) == 1 else ""),
        )
    else:
        ctx["l2"] = l2_single

    if extra_ctx:
        # Additive per-route extras (2026-07-10; e.g. the distributions
        # page's export-popup L2 options). Templates that don't reference
        # the keys render identically.
        ctx.update(extra_ctx)

    return render_template(template, **ctx) if render else ctx


# ─── The overview/pivot builder ──────────────────────────────────────────────

def build_pivot(db, request, *, owner, trx_type, year_default,
                template=None,
                payment_cash_basis=False,
                extra_where="",
                cat_l1_type=None,
                cat_l1_extra="",
                l2_fallback="Uncategorized",
                top_vendor_mode=None,
                years_owner=None,
                by_l2=False,
                render=True,
                date_from=None, date_to=None, date_field=None):
    """Parameterized overview/pivot route body (monthly pivot + stat cards).

    Two shapes:
      by_l2=False (default)  L1 × month pivot. Row list = canonical L1s from
                             the categories table (`cat_l1_type`, optionally
                             narrowed by `cat_l1_extra`, e.g. income's
                             " AND l1 != 'Taxes'"). Includes the L2 breakdown
                             (l2_map + l2_monthly_flat) and the zero-sum
                             row-hiding pass (2026-06-29).
      by_l2=True             Pinned-L1-style L2 × month pivot. Row list =
                             DISTINCT L2s actually present in the filtered
                             transactions. Adds grand + monthly_totals; no
                             l2_map/top_vendor/zero-hiding (originals had none).

    Flags (all preserve original per-route behavior exactly):
      payment_cash_basis    payment-date cash basis: dates on
                        COALESCE(payment_date, trx_date) everywhere
                        (pivot months, year filter, months_with_data,
                        available_years). False → trx_date.
      extra_where       extra SQL condition ANDed into the transactions WHERE
                        (e.g. income excludes Taxes; a pinned-L1 variant
                        pins its L1).
      l2_fallback       label for NULL l2_category (default 'Uncategorized').
      top_vendor_mode   None → no top_vendor context var (income).
                        'sum'   → COALESCE(vendor, raw_description)
                                  ranked by SUM(amount).
                        'count' → vendor ranked by COUNT(*).
      years_owner       owner scope for the year selector.
      render            False → return the context dict instead of rendering
                        (income/overview extends it with its Taxes + Net
                        tables before rendering itself).
      date_from/date_to EXPORT MODE (2026-07-10, additive): when BOTH are set
                        ('YYYY-MM-DD'), the year window becomes
                        `date_expr BETWEEN ? AND ?` and month buckets become
                        'YYYY-MM' keys spanning the range (ctx gains
                        `month_keys`). Absent → exact legacy behavior
                        ('01'..'12' buckets, year filter).
      date_field        'trx'|'post'|'statement'|'payment' basis for export
                        mode. Default: 'payment' when payment_cash_basis else 'trx'.
    """
    year = request.args.get("year", year_default)

    ranged = bool(date_from and date_to)
    if ranged:
        _basis = date_field or ("payment" if payment_cash_basis else "trx")
        date_expr = date_field_expr(_basis, alias="")
    else:
        date_expr = ("COALESCE(payment_date, trx_date)" if payment_cash_basis
                     else "trx_date")
    conds = [f"owner='{owner}'", f"trx_type='{trx_type}'", "status='active'"]
    if extra_where:
        conds.append(extra_where)
    if ranged:
        conds.append(f"{date_expr} BETWEEN ? AND ?")
        _wparams = (date_from, date_to)
        month_fmt = "%Y-%m"
        month_keys = month_span(date_from, date_to)
    else:
        conds.append(f"strftime('%Y', {date_expr})=?")
        _wparams = (year,)
        month_fmt = "%m"
        month_keys = [str(m).zfill(2) for m in range(1, 13)]
    base = " AND ".join(conds)

    # ── Stat cards + year selector (shared by both shapes) ────────────────
    trx_count = db.execute(
        f"SELECT COUNT(*) FROM transactions WHERE {base}", _wparams
    ).fetchone()[0]
    # Months with at least one trx — cash-basis pages run payment-date, so
    # use the same COALESCE the rest of the page uses.
    months_with_data = db.execute(f"""
        SELECT COUNT(DISTINCT strftime('{month_fmt}', {date_expr}))
        FROM transactions WHERE {base}
    """, _wparams).fetchone()[0]
    available_years = get_available_years(db, owner=years_owner,
                                          date_expr=date_expr)

    if by_l2:
        # ── Pinned-L1 shape: monthly pivot by L2 ──────────────────────────
        rows = db.execute(f"""
            SELECT strftime('{month_fmt}', {date_expr}) AS month,
                   COALESCE(l2_category,'{l2_fallback}') AS l2_category,
                   SUM(amount) AS total
              FROM transactions WHERE {base}
             GROUP BY month, l2_category
             ORDER BY l2_category, month
        """, _wparams).fetchall()

        # Pull list of L2s present this year so columns are correct
        l2_list = db.execute(f"""
            SELECT DISTINCT COALESCE(l2_category,'{l2_fallback}') AS l2
              FROM transactions WHERE {base}
             ORDER BY l2
        """, _wparams).fetchall()
        l2s = [r["l2"] for r in l2_list]

        pivot  = {l2: {mk: 0.0 for mk in month_keys} for l2 in l2s}
        for r in rows:
            if r["l2_category"] in pivot:
                pivot[r["l2_category"]][r["month"]] = r["total"]
        totals = {l2: sum(pivot[l2].values()) for l2 in l2s}
        grand  = sum(totals.values())

        # Monthly column totals
        monthly_totals = {mk: 0.0 for mk in month_keys}
        for l2 in l2s:
            for m in monthly_totals:
                monthly_totals[m] += pivot[l2][m]

        ctx = dict(
            year=year, l2s=l2s, pivot=pivot, totals=totals, grand=grand,
            monthly_totals=monthly_totals, month_keys=month_keys,
            trx_count=trx_count, months_with_data=months_with_data,
            available_years=available_years or [year],
        )
        return render_template(template, **ctx) if render else ctx

    # ── L1 shape: main pivot — by L1 × month ──────────────────────────────
    rows = db.execute(f"""
        SELECT strftime('{month_fmt}', {date_expr}) as month, l1_category, SUM(amount) as total
        FROM transactions WHERE {base}
        GROUP BY month, l1_category ORDER BY month, l1_category
    """, _wparams).fetchall()

    l2_rows = db.execute(f"""
        SELECT l1_category, l2_category, SUM(amount) as total, COUNT(*) as cnt
        FROM transactions WHERE {base}
        GROUP BY l1_category, l2_category ORDER BY l1_category, total DESC
    """, _wparams).fetchall()

    l2_monthly_rows = db.execute(f"""
        SELECT l1_category, l2_category, strftime('{month_fmt}', {date_expr}) as month,
               SUM(amount) as total
        FROM transactions WHERE {base}
        GROUP BY l1_category, l2_category, month
    """, _wparams).fetchall()
    l2_monthly_flat = {}
    for r in l2_monthly_rows:
        k = f"{r['l1_category']}||{r['l2_category'] or l2_fallback}||{r['month']}"
        l2_monthly_flat[k] = r["total"]

    l1_rows = db.execute(
        f"SELECT DISTINCT l1 FROM categories WHERE trx_type='{cat_l1_type}'{cat_l1_extra} "
        # Salary & Wages pinned first on income tables (fixed sort rule); rest A-Z.
        f"ORDER BY (l1 != 'Salary & Wages'), l1"
    ).fetchall()
    l1_list = [r["l1"] for r in l1_rows]

    pivot  = {l1: {mk: 0.0 for mk in month_keys} for l1 in l1_list}
    for r in rows:
        if r["l1_category"] in pivot:
            pivot[r["l1_category"]][r["month"]] = r["total"]
    totals = {l1: sum(pivot[l1].values()) for l1 in l1_list}

    l2_map = {}
    for r in l2_rows:
        l2_map.setdefault(r["l1_category"], []).append({
            "l2": r["l2_category"] or l2_fallback,
            "total": r["total"], "cnt": r["cnt"],
        })

    top_vendor = None
    if top_vendor_mode == "sum":
        # Biggest vendor by dollars spent
        row = db.execute(f"""
            SELECT COALESCE(vendor, raw_description) as v, SUM(amount) as t
            FROM transactions WHERE {base}
            GROUP BY v ORDER BY t DESC LIMIT 1
        """, _wparams).fetchone()
        top_vendor = row["v"] if row else None
    elif top_vendor_mode == "count":
        # Most frequent vendor by transaction count
        row = db.execute(f"""
            SELECT vendor, COUNT(*) as cnt FROM transactions WHERE {base}
            GROUP BY vendor ORDER BY cnt DESC LIMIT 1
        """, _wparams).fetchone()
        top_vendor = row["vendor"] if row else None

    # ── Zero-state (brand-new ledger) vs. the normal auto-hide pass ──────────
    # When the ledger holds NO non-deleted transactions of this flow at all
    # (any year — not just the selected one), render the FULL canonical
    # category tree at $0 so a new user sees the real table shapes before
    # their first import. The moment any data exists, behavior reverts to
    # the zero-row auto-hide below. Implemented here as a branch — the pivot
    # math above is untouched.
    zero_state = db.execute(
        f"""SELECT 1 FROM transactions
             WHERE owner='{owner}' AND trx_type='{trx_type}' AND status='active'
             LIMIT 1"""
    ).fetchone() is None
    if zero_state:
        # Full canonical L2 tree under each L1, all at $0 / 0 trx.
        l2_map = {}
        for r in db.execute(
            f"SELECT l1, l2 FROM categories WHERE trx_type='{cat_l1_type}'"
            f"{cat_l1_extra} ORDER BY l1, l2"
        ).fetchall():
            l2_map.setdefault(r["l1"], []).append(
                {"l2": r["l2"] or l2_fallback, "total": 0.0, "cnt": 0})
        # l1_list stays the full canonical list (no hiding pass).
    else:
        # Hide zero-sum L1 rows + zero-sum L2s within the rest. The legacy
        # 'Profit Distributions' exclusion is a harmless no-op on the starter
        # category tree; kept for behavior parity. (2026-06-29.)
        # NOTE: `pivot`/`totals` deliberately keep ALL canonical L1s (originals
        # filtered only l1_list + l2_map) — income's Net table depends on that.
        l1_list = [l1 for l1 in l1_list
                   if round(totals.get(l1, 0) or 0, 2) != 0 and l1 != 'Profit Distributions']
        l2_map = {l1: [x for x in items if round((x['total'] or 0), 2) != 0]
                  for l1, items in l2_map.items() if l1 in l1_list}

    ctx = dict(
        year=year, l1_list=l1_list, pivot=pivot, totals=totals,
        l2_map=l2_map, l2_monthly_flat=l2_monthly_flat, month_keys=month_keys,
        trx_count=trx_count, months_with_data=months_with_data,
        available_years=available_years or [year],
        zero_state=zero_state,
    )
    if top_vendor_mode:
        ctx["top_vendor"] = top_vendor
    return render_template(template, **ctx) if render else ctx
