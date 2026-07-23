"""
routes_tools.py — tools home, data cleanup, actuals-vs-budget, import landing.

Part of Personal Financial Tracker (PFT). No blueprints:
register(app, helpers) binds every view under its original function
name, so endpoint names, url_for(...) and base.html `ep ==` checks are
unchanged.
"""
from datetime import date, timedelta
from flask import request, render_template, jsonify
from config import CURRENT_YEAR, OWNER, TOOLS_REGISTRY
from db import get_db, get_setting, set_setting


# ─── Data Cleanup tool ───────────────────────────────────────────────────────
# One page of judgment-needed review lists, grouped by correction type.
# Detection re-runs on every load, so this doubles as the recurring
# vendor/tag/dupe review surface.

def _cleanup_ignored(db) -> set:
    """Keys the user has dismissed — never show these findings again.
    Key formats: vendor:<name> · tagmerge:<id> · tagunused:<id> ·
    dates:<trx_id> · dup:<ids> · cat:<trx_id> · rcpt:<trx_id>"""
    import json as _json
    try:
        return set(_json.loads(get_setting(db, "cleanup_ignored", "[]")))
    except ValueError:
        return set()


def _cleanup_scan(db):
    """All cleanup detection queries. Read-only; returns a dict of findings.
    Findings are grouped by correction TYPE; each item is reviewed/edited/
    saved individually and can be ignored forever."""
    from vendor_rules import strip_noise
    out = {}
    ignored = _cleanup_ignored(db)
    out["ignored_count"] = len(ignored)

    # 1. VENDORS — one list: auto-cleanable names (suggestion prefilled from
    # strip_noise) + names that don't read like an entity (blank suggestion).
    import re as _re
    vendors = []
    for r in db.execute("""SELECT vendor, COUNT(*) n FROM transactions
                           WHERE vendor IS NOT NULL AND status='active'
                           GROUP BY vendor"""):
        v = r["vendor"]
        if f"vendor:{v}" in ignored:
            continue
        clean = strip_noise(v)
        if clean and clean != v:
            vendors.append({"vendor": v, "suggested": clean, "n": r["n"],
                            "key": f"vendor:{v}"})
        elif (_re.search(r"\d{5,}", v) or "*" in v or " Id:" in v
              or _re.search(r"\b(Llc|Svc|Bkg|Acctverify|Moneyline)\b", v)):
            vendors.append({"vendor": v, "suggested": "", "n": r["n"],
                            "key": f"vendor:{v}"})
    out["vendor_fixes"] = sorted(vendors, key=lambda x: -x["n"])

    # 2. TAGS — merge/consolidation suggestions only. Suspected same-tag:
    # case variants (Trip/trip), space variants (gym membership/gymmembership),
    # and prefix variants (gym/gym membership).
    tags = db.execute("""SELECT t.id, t.name,
                         (SELECT COUNT(*) FROM transaction_tags tt WHERE tt.tag_id=t.id) n
                         FROM tags t""").fetchall()

    def _same_tag(a, b):
        x, y = a.lower().strip(), b.lower().strip()
        if x == y or x.replace(" ", "") == y.replace(" ", ""):
            return True
        s, l = (x, y) if len(x) <= len(y) else (y, x)
        return len(s) >= 3 and l.replace(" ", "").startswith(s.replace(" ", ""))

    clusters = []
    for t in tags:
        placed = False
        for cl in clusters:
            if any(_same_tag(t["name"], o["name"]) for o in cl):
                cl.append(t); placed = True
                break
        if not placed:
            clusters.append([t])
    tag_merges = []
    for cl in clusters:
        if len(cl) < 2:
            continue
        keep = max(cl, key=lambda t: t["n"])
        for t in cl:
            if t["id"] != keep["id"] and f"tagmerge:{t['id']}" not in ignored:
                tag_merges.append({"from_id": t["id"], "from": t["name"], "from_n": t["n"],
                                   "into_id": keep["id"], "into": keep["name"], "into_n": keep["n"],
                                   "key": f"tagmerge:{t['id']}"})
    out["tag_merges"] = tag_merges

    # 3. MISSING DATES — which date fields are empty, per row
    md = []
    for r in db.execute("""
        SELECT id, owner, vendor, trx_date, amount,
               payment_date IS NULL np, statement_date IS NULL ns,
               post_date IS NULL npo
        FROM transactions
        WHERE status='active' AND (payment_date IS NULL OR statement_date IS NULL
                                   OR post_date IS NULL)"""):
        if f"dates:{r['id']}" in ignored:
            continue
        missing = [n for n, f in (("payment", r["np"]), ("statement", r["ns"]),
                                  ("post", r["npo"])) if f]
        md.append({"id": r["id"], "owner": r["owner"], "vendor": r["vendor"],
                   "trx_date": r["trx_date"], "amount": r["amount"],
                   "missing": missing, "key": f"dates:{r['id']}"})
    out["missing_dates"] = md

    # 4. POSSIBLE DUPLICATES — same account/day/SIGNED amount/description.
    # (Signed: a credit and a debit of the same magnitude are opposite flows,
    # not duplicates.) SAME-IMPORT exclusion: banks rarely duplicate a row
    # within one export, so a group whose rows all came from the SAME import
    # batch is real repeat spending (subway swipes, two drinks at the bar) —
    # not flagged. Only cross-batch or manual-entry groups are suspects.
    dups = []
    for r in db.execute("""
        SELECT MIN(t.vendor) vendor, t.trx_date, ROUND(t.amount,2) amt,
               COUNT(*) n, GROUP_CONCAT(t.id) ids,
               COUNT(DISTINCT COALESCE(s.import_batch_id, -t.id)) n_batches
        FROM transactions t LEFT JOIN staging s ON s.id = t.staging_id
        WHERE t.status='active'
        GROUP BY t.account_id, t.trx_date, ROUND(t.amount,2), t.trx_type,
                 REPLACE(UPPER(t.raw_description),' ','')
        HAVING COUNT(*) > 1 ORDER BY ABS(amt) DESC"""):
        if r["n_batches"] <= 1:
            continue  # all rows from one import → legitimately repeated
        key = f"dup:{r['ids']}"
        if key in ignored:
            continue
        d = dict(r); d["key"] = key
        dups.append(d)
    out["dup_groups"] = dups

    # 5. CATEGORIES — L1/L2 missing or not in the canonical category tree.
    # Fixable inline: each row gets the L1/L2 dropdowns + Save.
    cats = []
    for r in db.execute("""
        SELECT t.id, t.owner, t.trx_type, t.vendor, t.amount, t.trx_date,
               t.l1_category, t.l2_category
        FROM transactions t WHERE t.status='active'
        AND (t.l1_category IS NULL OR NOT EXISTS (
            SELECT 1 FROM categories c WHERE c.l1=t.l1_category
            AND (c.l2=t.l2_category OR t.l2_category IS NULL)
            AND c.trx_type = t.trx_type))
        ORDER BY t.trx_date"""):
        if f"cat:{r['id']}" in ignored:
            continue
        d = dict(r); d["key"] = f"cat:{r['id']}"
        cats.append(d)
    out["bad_cats"] = cats

    # 6. MISSING RECEIPTS — every active expense with no receipt and no
    # no-receipt-needed flag. Resolve by inboxing the receipt (scan → match)
    # or clicking "No receipt needed" right here.
    missing_rcpt = []
    for r in db.execute("""
        SELECT id, vendor, trx_date, amount, l1_category, l2_category
        FROM transactions
        WHERE status='active' AND owner=? AND trx_type='expense'
          AND (receipt_path IS NULL OR receipt_path='')
          AND COALESCE(no_receipt_needed,0)=0
        ORDER BY trx_date DESC""", (OWNER,)):
        if f"rcpt:{r['id']}" in ignored:
            continue
        d = dict(r); d["key"] = f"rcpt:{r['id']}"
        missing_rcpt.append(d)
    out["missing_receipts"] = missing_rcpt

    # Category map for the inline pickers (cat_type → {l1: [l2...]})
    cat_map = {}
    for c in db.execute("SELECT trx_type,l1,l2 FROM categories ORDER BY trx_type,l1,l2"):
        cat_map.setdefault(c["trx_type"], {}).setdefault(c["l1"], []).append(c["l2"])
    out["cat_map"] = cat_map

    return out


def tools_cleanup():
    """Tools — Data Cleanup. Per-item review lists grouped by correction
    type."""
    db = get_db()
    scan = _cleanup_scan(db)
    return render_template("tools/cleanup.html", scan=scan)


def api_cleanup_ignore():
    """Dismiss a finding forever. Body: {key: 'vendor:...'|'dup:...'|...}.
    {clear: true} wipes the whole ignore list (un-hides everything)."""
    import json as _json
    data = request.get_json(silent=True) or {}
    db = get_db()
    if data.get("clear"):
        set_setting(db, "cleanup_ignored", "[]")
        db.commit()
        return jsonify({"ok": True, "ignored": 0})
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "key required"}), 400
    ignored = _cleanup_ignored(db)
    ignored.add(key)
    set_setting(db, "cleanup_ignored", _json.dumps(sorted(ignored)))
    db.commit()
    return jsonify({"ok": True, "ignored": len(ignored)})


def api_cleanup_merge_tag():
    """Merge one tag into another. Body: {from_id, into_id}."""
    data = request.get_json(silent=True) or {}
    from_id, into_id = data.get("from_id"), data.get("into_id")
    if not from_id or not into_id or from_id == into_id:
        return jsonify({"ok": False, "error": "need distinct from_id/into_id"}), 400
    db = get_db()
    db.execute("""INSERT OR IGNORE INTO transaction_tags (trx_id, tag_id)
                  SELECT trx_id, ? FROM transaction_tags WHERE tag_id=?""",
               (into_id, from_id))
    db.execute("DELETE FROM transaction_tags WHERE tag_id=?", (from_id,))
    db.execute("DELETE FROM tags WHERE id=?", (from_id,))
    db.commit()
    return jsonify({"ok": True})


def api_cleanup_fix_dates():
    """Fill a single trx's missing payment/statement/post dates from its
    trx_date (the same fallback the importer uses). Body: {trx_id}."""
    data = request.get_json(silent=True) or {}
    tid = data.get("trx_id")
    if not tid:
        return jsonify({"ok": False, "error": "trx_id required"}), 400
    db = get_db()
    n = db.execute("""UPDATE transactions
        SET payment_date=COALESCE(payment_date, trx_date),
            statement_date=COALESCE(statement_date, trx_date),
            post_date=COALESCE(post_date, trx_date),
            updated_at=datetime('now')
        WHERE id=? AND status='active'""", (tid,)).rowcount
    db.commit()
    return jsonify({"ok": bool(n), "fixed": n})


def api_cleanup_rename_vendor():
    """Global vendor rename. Body: {from: 'Brokerage Svc Llc…', to: 'My Brokerage'}."""
    data = request.get_json(silent=True) or {}
    src = (data.get("from") or "").strip()
    dst = (data.get("to") or "").strip()
    if not src or not dst or src == dst:
        return jsonify({"ok": False, "error": "need distinct from/to"}), 400
    db = get_db()
    n = db.execute("UPDATE transactions SET vendor=? WHERE vendor=?", (dst, src)).rowcount
    db.execute("UPDATE staging SET vendor=? WHERE vendor=?", (dst, src))
    db.commit()
    return jsonify({"ok": True, "renamed": n})


def api_cleanup_delete_tag():
    """Delete a tag ONLY if it has zero uses."""
    data = request.get_json(silent=True) or {}
    tag_id = data.get("tag_id")
    db = get_db()
    used = db.execute("SELECT COUNT(*) FROM transaction_tags WHERE tag_id=?",
                      (tag_id,)).fetchone()[0]
    if used:
        return jsonify({"ok": False, "error": f"tag is used on {used} transactions"}), 400
    db.execute("DELETE FROM tags WHERE id=?", (tag_id,))
    db.commit()
    return jsonify({"ok": True})


def api_cleanup_no_receipt():
    """Flag selected trx ids as no_receipt_needed. Body: {trx_ids: [..]}."""
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in (data.get("trx_ids") or []) if str(i).isdigit()]
    if not ids:
        return jsonify({"ok": False, "error": "trx_ids required"}), 400
    db = get_db()
    ph = ",".join("?" * len(ids))
    n = db.execute(f"""UPDATE transactions SET no_receipt_needed=1,
                       updated_at=datetime('now') WHERE id IN ({ph})""", ids).rowcount
    db.commit()
    return jsonify({"ok": True, "flagged": n})


# ─── Tools: home + Import landing page ───────────────────────────────────────
#
# Tools is a top-level menu group housing one-off operational utilities.
# The Import landing page sends users to the existing leaf pages — those
# URLs stay intact so all in-app links and bookmarks continue to work.

def tools_home():
    """All Tools — one card per tool, built or not, straight from
    TOOLS_REGISTRY."""
    cards = [{"title": t["title"], "desc": t["desc"], "note": t["note"],
              "built": t["built"], "url": t["url"].get(OWNER),
              "portals": t["portals"]}
             for t in TOOLS_REGISTRY if OWNER in t["portals"]]
    return render_template("tools/tools_home.html", cards=cards)


def tools_import():
    """Tools — Import landing. Two cards: CSV Upload and Manual Entry.
    Each links to the existing import page."""
    return render_template("tools/import_home.html")


def tools_actuals_vs_budget():
    """Tools → Actuals vs. Budget.

    Two views answer the same question — "am I on track against what I thought
    I was going to spend?" — at different time-framings:

      • PACE (default) — YTD actuals vs. PRORATED YTD budget (annual / 12 × N
                         complete months). The "am I on pace?" view.
      • ANNUAL         — YTD actuals vs. FULL annual budget, with the year-
                         elapsed % shown as a reference marker. The "how much
                         room do I still have?" view.

    Mechanics:
      - Months-complete = current_month - 1 for the current year; 12 for past
        years. This pins actuals to whole-month boundaries so the prorate is
        comparable.
      - Budgets: L1-level rows only (budget_values WHERE l2 IS NULL).
      - L1 set is the union of (categories table, budget_values, observed
        actuals) so nothing gets dropped silently.
      - Sort: worst pace variance first (biggest over-spenders surface).
    """
    db = get_db()
    portal = OWNER

    # ── Year + view selection ──────────────────────────────────────────────
    try:
        year = int(request.args.get("year", CURRENT_YEAR))
    except (TypeError, ValueError):
        year = int(CURRENT_YEAR)

    view = request.args.get("view", "pace")
    if view not in ("pace", "annual"):
        view = "pace"

    sort_by = request.args.get("sort", "variance")
    if sort_by not in ("variance", "alpha", "spend"):
        sort_by = "variance"

    # ── Period framing: months_complete + as_of_date + prorate ─────────────
    today = date.today()
    cur_year = today.year
    if year < cur_year:
        months_complete = 12
        as_of_date = date(year, 12, 31)
    elif year > cur_year:
        months_complete = 0
        as_of_date = date(year, 1, 1)
    else:
        # Current year. Use the last fully-complete month. If we're in
        # January, fall back to including January-to-date (fractional)
        # so the page isn't useless on Jan 1-31.
        if today.month == 1:
            # Days into Jan / 31 — a rough fractional month
            months_complete = today.day / 31.0
            as_of_date = today
        else:
            months_complete = today.month - 1
            # Last day of the previous month
            as_of_date = date(year, today.month, 1) - timedelta(days=1)

    prorate = months_complete / 12.0
    # % of calendar year elapsed (used as a reference marker on Annual view)
    if year < cur_year:
        year_elapsed_pct = 100.0
    elif year > cur_year:
        year_elapsed_pct = 0.0
    else:
        # Day-of-year / 365 (close enough; 366 in a leap year — fine for a viz)
        year_elapsed_pct = (today - date(cur_year, 1, 1)).days / 365.0 * 100

    # ── Budgets — both levels. L2 budgets ROLL UP: when any L2 under an L1
    # has a budget, that L1's effective budget = Σ its L2 rows (the flat L1
    # row is ignored). Otherwise the flat L1 row applies.
    flat_budgets = {}          # l1 → flat L1-level amount
    l2_budgets = {}            # l1 → {l2: amount}
    for r in db.execute("""
        SELECT l1, l2, amount FROM budget_values
         WHERE portal=? AND year=?
    """, (portal, year)).fetchall():
        if r["l2"]:
            if r["amount"]:
                l2_budgets.setdefault(r["l1"], {})[r["l2"]] = r["amount"]
        else:
            flat_budgets[r["l1"]] = r["amount"]
    budgets = dict(flat_budgets)
    for l1, m in l2_budgets.items():
        budgets[l1] = round(sum(m.values()), 2)   # rollup wins

    # ── Actuals (through as_of_date) ───────────────────────────────────────
    actuals_rows = db.execute("""
        SELECT l1_category AS l1, SUM(amount) AS total
          FROM transactions
         WHERE owner=? AND trx_type='expense' AND status='active'
           AND strftime('%Y', trx_date) = ?
           AND date(trx_date) <= ?
         GROUP BY l1_category
    """, (OWNER, str(year), as_of_date.isoformat())).fetchall()
    actuals = {(r["l1"] or "Uncategorized"): (r["total"] or 0.0) for r in actuals_rows}

    # Per-L2 actuals — only needed for L1s that have L2-level budgets.
    l2_actuals = {}            # (l1, l2) → total
    if l2_budgets:
        for r in db.execute("""
            SELECT l1_category AS l1,
                   COALESCE(l2_category, 'Uncategorized') AS l2,
                   SUM(amount) AS total
              FROM transactions
             WHERE owner=? AND trx_type='expense' AND status='active'
               AND strftime('%Y', trx_date) = ?
               AND date(trx_date) <= ?
             GROUP BY l1_category, l2_category
        """, (OWNER, str(year), as_of_date.isoformat())).fetchall():
            l2_actuals[(r["l1"], r["l2"])] = r["total"] or 0.0

    # ── L1 universe: budget + actuals + canonical categories ───────────────
    l1_rows = db.execute("""
        SELECT DISTINCT l1 FROM categories
         WHERE trx_type='expense'
           AND l1 IS NOT NULL AND l1 != ''
    """).fetchall()
    # Income L1s live in the same budget_values table — they get their OWN
    # card below, so keep them out of the expense universe.
    income_l1s = [r["l1"] for r in db.execute(
        "SELECT DISTINCT l1 FROM categories WHERE trx_type='income' "
        "AND l1 IS NOT NULL AND l1 != '' ORDER BY l1")]

    l1_set = {r["l1"] for r in l1_rows} | set(budgets.keys()) | set(actuals.keys())
    l1_set -= set(income_l1s)
    # Quietly drop the Uncategorized bucket UNLESS there are uncategorized
    # actuals — in which case surface them so the user can see + fix.
    if "Uncategorized" in l1_set and actuals.get("Uncategorized", 0) == 0:
        l1_set.discard("Uncategorized")

    rows = []
    for l1 in l1_set:
        annual_budget    = budgets.get(l1, 0.0)
        prorated_budget  = annual_budget * prorate
        actual           = actuals.get(l1, 0.0)

        pace_variance     = actual - prorated_budget  # +ve = over pace
        pace_variance_pct = (pace_variance / prorated_budget * 100) if prorated_budget else None
        annual_pct        = (actual / annual_budget * 100) if annual_budget else None

        # Status classification (Pace view).
        # under = green, near = yellow, over = red.
        if not annual_budget:
            status = "nobudget"
        elif pace_variance_pct is None or pace_variance_pct <= 0:
            status = "under"
        elif pace_variance_pct <= 25:
            status = "near"
        else:
            status = "over"

        # L2 detail sub-rows for L1s with L2-level budgets (rollup members
        # + any spend in unbudgeted L2s of the same L1, so nothing hides).
        l2_detail = []
        if l1 in l2_budgets:
            seen = set(l2_budgets[l1].keys())
            observed = {k[1] for k in l2_actuals if k[0] == l1}
            for l2 in sorted(seen | observed):
                ab2 = l2_budgets[l1].get(l2, 0.0)
                pb2 = ab2 * prorate
                act2 = l2_actuals.get((l1, l2), 0.0)
                var2 = act2 - pb2
                if not ab2:
                    st2 = "nobudget"
                elif var2 <= 0:
                    st2 = "under"
                elif pb2 and var2 / pb2 * 100 <= 25:
                    st2 = "near"
                else:
                    st2 = "over"
                l2_detail.append({
                    "l2": l2, "actual": act2, "annual_budget": ab2,
                    "prorated_budget": pb2, "pace_variance": var2,
                    "pace_variance_pct": (var2 / pb2 * 100) if pb2 else None,
                    "annual_pct": (act2 / ab2 * 100) if ab2 else None,
                    "status": st2,
                })

        rows.append({
            "l1":                 l1,
            "actual":             actual,
            "annual_budget":      annual_budget,
            "prorated_budget":    prorated_budget,
            "pace_variance":      pace_variance,
            "pace_variance_pct":  pace_variance_pct,
            "annual_pct":         annual_pct,
            "status":             status,
            "l2_detail":          l2_detail,
        })

    # Sort
    if sort_by == "alpha":
        rows.sort(key=lambda r: r["l1"].lower())
    elif sort_by == "spend":
        rows.sort(key=lambda r: -r["actual"])
    else:  # variance — worst-over first, then no-budget rows, then alpha
        rows.sort(key=lambda r: (
            0 if r["status"] != "nobudget" else 1,
            -(r["pace_variance"] or 0),
            r["l1"].lower(),
        ))

    # Totals
    total_actual           = sum(r["actual"]           for r in rows)
    total_annual_budget    = sum(r["annual_budget"]    for r in rows)
    total_prorated_budget  = total_annual_budget * prorate
    total_pace_variance    = total_actual - total_prorated_budget
    total_pace_variance_pct = (total_pace_variance / total_prorated_budget * 100) if total_prorated_budget else None
    total_annual_pct       = (total_actual / total_annual_budget * 100) if total_annual_budget else None

    # ── Income section ────────────────────────────────────────────────────
    # Same framing as expenses but INVERTED goodness: ahead of pace = green.
    inc_rows_db = db.execute("""
        SELECT l1_category AS l1, SUM(amount) AS total FROM transactions
         WHERE owner=? AND trx_type='income' AND status='active'
           AND strftime('%Y', trx_date) = ?
           AND date(trx_date) <= ?
         GROUP BY l1_category
    """, (OWNER, str(year), as_of_date.isoformat())).fetchall()
    inc_actuals = {(r["l1"] or "Uncategorized"): (r["total"] or 0.0)
                   for r in inc_rows_db}
    inc_budgets = {l1: budgets.get(l1, 0.0) for l1 in income_l1s}

    income_rows = []
    for l1 in income_l1s:
        ab  = inc_budgets.get(l1, 0.0)
        pb  = ab * prorate
        act = inc_actuals.get(l1, 0.0)
        var = act - pb                      # positive = AHEAD (good)
        var_pct = (var / pb * 100) if pb else None
        if not ab:
            st = "nobudget"
        elif var >= 0:
            st = "under"                    # green — at/above pace
        elif var_pct is not None and var_pct >= -25:
            st = "near"
        else:
            st = "over"                     # red — well behind pace
        income_rows.append({
            "l1": l1, "actual": act, "annual_budget": ab,
            "prorated_budget": pb, "pace_variance": var,
            "pace_variance_pct": var_pct,
            "annual_pct": (act / ab * 100) if ab else None,
            "status": st,
        })
    inc_tot_actual = sum(r["actual"] for r in income_rows)
    inc_tot_annual = sum(r["annual_budget"] for r in income_rows)
    income_totals = {
        "actual": inc_tot_actual, "annual_budget": inc_tot_annual,
        "prorated_budget": inc_tot_annual * prorate,
        "pace_variance": inc_tot_actual - inc_tot_annual * prorate,
    }

    # Year selector — union of budget years + transaction years + current year
    year_rows = db.execute("""
        SELECT DISTINCT year FROM budget_values WHERE portal=?
    """, (portal,)).fetchall()
    trx_years = db.execute("""
        SELECT DISTINCT CAST(strftime('%Y', trx_date) AS INTEGER) AS y
          FROM transactions
         WHERE owner=? AND trx_type='expense' AND status='active'
    """, (OWNER,)).fetchall()
    available_years = sorted(
        {r["year"] for r in year_rows} |
        {r["y"] for r in trx_years if r["y"]} |
        {cur_year},
        reverse=True,
    )

    return render_template("tools/actuals_vs_budget.html",
        portal=portal,
        year=year,
        view=view,
        sort_by=sort_by,
        months_complete=months_complete,
        prorate_pct=prorate * 100,
        year_elapsed_pct=year_elapsed_pct,
        as_of_date=as_of_date,
        rows=rows,
        totals={
            "actual":              total_actual,
            "annual_budget":       total_annual_budget,
            "prorated_budget":     total_prorated_budget,
            "pace_variance":       total_pace_variance,
            "pace_variance_pct":   total_pace_variance_pct,
            "annual_pct":          total_annual_pct,
        },
        available_years=available_years,
        income_rows=income_rows,
        income_totals=income_totals,
    )


def register(app, helpers):
    """Bind this module's routes. Endpoint names == function names,
    exactly as in the pre-split app.py."""
    login_required = helpers["login_required"]
    global tools_cleanup, api_cleanup_ignore, api_cleanup_merge_tag, \
        api_cleanup_fix_dates, api_cleanup_rename_vendor, \
        api_cleanup_delete_tag, api_cleanup_no_receipt, tools_home, \
        tools_import, tools_actuals_vs_budget
    tools_cleanup = login_required(tools_cleanup)
    app.route("/tools/cleanup")(tools_cleanup)
    api_cleanup_ignore = login_required(api_cleanup_ignore)
    app.route("/api/cleanup/ignore", methods=["POST"])(api_cleanup_ignore)
    api_cleanup_merge_tag = login_required(api_cleanup_merge_tag)
    app.route("/api/cleanup/merge-tag", methods=["POST"])(api_cleanup_merge_tag)
    api_cleanup_fix_dates = login_required(api_cleanup_fix_dates)
    app.route("/api/cleanup/fix-dates", methods=["POST"])(api_cleanup_fix_dates)
    api_cleanup_rename_vendor = login_required(api_cleanup_rename_vendor)
    app.route("/api/cleanup/rename-vendor", methods=["POST"])(api_cleanup_rename_vendor)
    api_cleanup_delete_tag = login_required(api_cleanup_delete_tag)
    app.route("/api/cleanup/delete-tag", methods=["POST"])(api_cleanup_delete_tag)
    api_cleanup_no_receipt = login_required(api_cleanup_no_receipt)
    app.route("/api/cleanup/no-receipt", methods=["POST"])(api_cleanup_no_receipt)
    tools_home = login_required(tools_home)
    app.route("/tools")(tools_home)
    tools_import = login_required(tools_import)
    app.route("/tools/import")(tools_import)
    tools_actuals_vs_budget = login_required(tools_actuals_vs_budget)
    app.route("/tools/actuals-vs-budget")(tools_actuals_vs_budget)
