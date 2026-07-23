"""
routes_settings.py — settings pages + APIs (categorization, assumptions,
budgets, FX, shortcuts, tools menu, security).

No blueprints: register(app, helpers) binds every view under its original
function name, so endpoint names, url_for(...) and base.html `ep ==` checks
are unchanged.
"""
import os
from flask import request, render_template, jsonify, redirect, url_for, flash
from config import (
    CURRENT_YEAR, BASE_DIR, OWNER, APP_PASSWORD,
    SHORTCUT_ACTIONS, DEFAULT_SHORTCUTS, TOOLS_REGISTRY, DEFAULT_TOOLS_MENU,
    FX_RATES,
)
from db import get_db, get_setting, set_setting


def get_shortcuts(db) -> dict:
    import json as _json
    raw = get_setting(db, "keyboard_shortcuts")
    merged = dict(DEFAULT_SHORTCUTS)
    if raw:
        try:
            saved = _json.loads(raw)
            merged.update({k: v for k, v in saved.items()
                           if k in SHORTCUT_ACTIONS and isinstance(v, str) and v})
        except ValueError:
            pass
    return merged


def get_tools_menu_visible(db, portal=OWNER) -> list:
    """Saved Tools-submenu keys, in display order. Single-portal build:
    the `portal` arg is accepted for caller compatibility and ignored."""
    import json as _json
    valid = {t["key"] for t in TOOLS_REGISTRY}
    raw = get_setting(db, "tools_menu_visible")
    if raw:
        try:
            return [k for k in _json.loads(raw) if k in valid]
        except ValueError:
            pass
    return list(DEFAULT_TOOLS_MENU)


def _tool_is_active(tool, endpoint) -> bool:
    if not endpoint:
        return False
    if tool.get("ep_prefix") and endpoint.startswith(tool["ep_prefix"]):
        return True
    return endpoint in tool.get("eps", [])


def get_fx_rate(db, currency: str) -> float:
    cfg = FX_RATES[currency]
    try:
        return float(get_setting(db, cfg["key"], cfg["default"]))
    except (TypeError, ValueError):
        return float(cfg["default"])


def settings_categorization():
    """Read-only view of the L1/L2 category trees (expense / income /
    transfer)."""
    db = get_db()
    types = ("expense", "income", "transfer")
    rows = db.execute(
        f"""SELECT trx_type, l1, l2 FROM categories
            WHERE trx_type IN ({','.join('?' * len(types))})
            ORDER BY trx_type, l1, l2""",
        types
    ).fetchall()

    # Group L2s under each L1, separately for expense vs income
    grouped = {}  # {trx_type: {l1: [l2, l2, ...]}}
    for r in rows:
        grouped.setdefault(r["trx_type"], {}).setdefault(r["l1"], []).append(r["l2"])

    return render_template("settings_categorization.html", grouped=grouped)


def api_categories_add():
    """Add categories from the portal. Body:
      {trx_type, l1, l2s: ["...", ...]}
    Rules: trx_type must be a real tree; a NEW L1 requires at least one L2;
    adding L2s to an existing L1 is fine. Writes BOTH the live categories
    table (usable immediately) AND custom_categories, which init_db re-applies
    after the wipe-and-reseed on every restart — so portal-added categories
    are permanent."""
    data = request.get_json(silent=True) or {}
    trx_type = (data.get("trx_type") or "").strip()
    l1 = (data.get("l1") or "").strip()
    l2s = [s.strip() for s in (data.get("l2s") or []) if s and s.strip()]
    if trx_type not in ("expense", "income", "transfer"):
        return jsonify({"ok": False, "error": "invalid category tree"}), 400
    if not l1:
        return jsonify({"ok": False, "error": "L1 name required"}), 400
    if not l2s:
        return jsonify({"ok": False, "error": "at least one L2 required"}), 400
    db = get_db()
    # Lazy-create (mirrors init_db) so this works even before a restart
    db.execute("""CREATE TABLE IF NOT EXISTS custom_categories (
        trx_type   TEXT NOT NULL,
        l1         TEXT NOT NULL,
        l2         TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(trx_type, l1, l2))""")
    exists = db.execute(
        "SELECT COUNT(*) FROM categories WHERE trx_type=? AND l1=?",
        (trx_type, l1)).fetchone()[0]
    added, dupes = 0, 0
    for l2 in l2s:
        cur = db.execute(
            "INSERT OR IGNORE INTO categories (trx_type, l1, l2) VALUES (?,?,?)",
            (trx_type, l1, l2))
        if cur.rowcount:
            added += 1
        else:
            dupes += 1
        db.execute(
            "INSERT OR IGNORE INTO custom_categories (trx_type, l1, l2) VALUES (?,?,?)",
            (trx_type, l1, l2))
    db.commit()
    return jsonify({"ok": True, "added": added, "duplicates": dupes,
                    "new_l1": not exists})


# ─── Docs & Settings: Overview / Assumptions / Security ──────────────────────
#
# These pages anchor the Docs & Settings nav group (alongside the
# Categorization page above).
#
#   • Overview      — docs page describing the tool, current features, and
#                     what's upcoming.
#   • Assumptions   — Landing page listing documented modeling assumptions
#                     with subpage links (Budget Values, Exchange Rates).
#   • Security      — username/password editing stub. Real impl deferred
#                     (needs DB-backed credentials + hashing).

def settings_overview():
    """Docs & Settings — Overview."""
    return render_template("settings/overview.html")


def settings_assumptions():
    """Docs & Settings — Assumptions landing page. Lists modeling-assumption
    topics and links to their dedicated subpages."""
    return render_template("settings/assumptions.html")


def settings_budget_values():
    """Docs & Settings → Assumptions → Budget Values. Year-scoped budget
    editor at two levels:

      • L1 row — the flat annual budget for a whole category group.
      • L2 rows — expand an L1 to budget its L2s individually. As soon as
        any L2 under an L1 has a non-zero budget, the L1's effective budget
        becomes the SUM of its L2 rows (the flat L1 input locks and shows
        the rolled-up total).

    Lists are sourced from the `categories` table at request time so any
    new L1/L2 added later is automatically picked up.
    """
    db = get_db()
    portal = OWNER

    # Year — default to current year. Year selector lets user view past years.
    try:
        year = int(request.args.get("year", CURRENT_YEAR))
    except (TypeError, ValueError):
        year = int(CURRENT_YEAR)

    # Canonical expense tree (L1 → [L2s]) from the live categories table.
    tree = {}
    for r in db.execute("""
        SELECT DISTINCT l1, l2 FROM categories
         WHERE trx_type = 'expense'
           AND l1 IS NOT NULL AND l1 != ''
         ORDER BY l1, l2
    """).fetchall():
        tree.setdefault(r["l1"], [])
        if r["l2"]:
            tree[r["l1"]].append(r["l2"])

    # Existing budget rows for this year — both levels.
    bv_map = {}    # l1 → flat L1 amount
    bv_l2 = {}     # (l1, l2) → amount
    for r in db.execute("""
        SELECT l1, l2, amount FROM budget_values
         WHERE portal = ? AND year = ?
    """, (portal, year)).fetchall():
        if r["l2"]:
            bv_l2[(r["l1"], r["l2"])] = r["amount"]
        else:
            bv_map[r["l1"]] = r["amount"]

    # Year selector range — every year that has a budget row + current year.
    year_rows = db.execute("""
        SELECT DISTINCT year FROM budget_values WHERE portal = ?
         ORDER BY year DESC
    """, (portal,)).fetchall()
    available_years = sorted(
        {r["year"] for r in year_rows} | {int(CURRENT_YEAR)},
        reverse=True
    )

    rows = []
    for l1, l2s in tree.items():
        l2_rows = [{"l2": l2, "amount": bv_l2.get((l1, l2), 0.0)} for l2 in l2s]
        l2_sum = round(sum(x["amount"] for x in l2_rows), 2)
        flat = bv_map.get(l1, 0.0)
        rows.append({
            "l1": l1,
            "amount": flat,
            "l2_rows": l2_rows,
            "l2_sum": l2_sum,
            "has_l2_budgets": l2_sum > 0,
            "effective": l2_sum if l2_sum > 0 else flat,
        })
    total = sum(r["effective"] for r in rows)

    # Income budgets — one editable line per income L1 (L1-level only).
    inc_l1s = [r["l1"] for r in db.execute("""
        SELECT DISTINCT l1 FROM categories
         WHERE trx_type='income' AND l1 IS NOT NULL AND l1 != ''
         ORDER BY l1""").fetchall()]
    income_rows = [{"l1": l1, "amount": bv_map.get(l1, 0.0)} for l1 in inc_l1s]

    return render_template("settings/budget_values.html",
        portal=portal,
        year=year,
        rows=rows,
        total=total,
        income_rows=income_rows,
        available_years=available_years,
    )


def api_budget_values_upsert():
    """Upsert a single budget value. JSON body: {year, l1, amount, l2?}.
    Omitted/empty l2 → the L1-level row. Returns {ok: true, amount: <float>}
    so the client can confirm the persisted value."""
    data = request.get_json(silent=True) or {}
    portal = OWNER  # single-portal build — any portal in the body is ignored

    try:
        year = int(data.get("year"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "year required"}), 400

    l1 = (data.get("l1") or "").strip()
    if not l1:
        return jsonify({"ok": False, "error": "l1 required"}), 400
    l2 = (data.get("l2") or "").strip()

    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "amount must be numeric"}), 400
    if amount < 0:
        amount = 0.0  # silently floor at zero — no negative budgets

    db = get_db()
    db.execute("""
        INSERT INTO budget_values (portal, year, l1, l2, amount, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(portal, year, l1, l2) DO UPDATE SET
            amount = excluded.amount,
            updated_at = datetime('now')
    """, (portal, year, l1, l2, amount))
    db.commit()
    return jsonify({"ok": True, "amount": amount})


def settings_security():
    """Docs & Settings — Security. Password-only auth; change form writes
    .app_password."""
    return render_template("settings/security.html")


def api_change_password():
    """Change the login password. Body: {current, new}.
    Verifies the current password, writes the new one to the gitignored
    .app_password file, and updates the running process — effective
    immediately, no restart. Recovery: the file itself (see Security page)."""
    global APP_PASSWORD
    data = request.get_json(silent=True) or {}
    if (data.get("current") or "") != APP_PASSWORD:
        return jsonify({"ok": False, "error": "Current password is wrong."}), 400
    new = (data.get("new") or "").strip()
    if not new:
        return jsonify({"ok": False, "error": "New password can't be empty."}), 400
    try:
        p = os.path.join(BASE_DIR, ".app_password")
        with open(p, "w") as f:
            f.write(new)
        os.chmod(p, 0o600)
    except OSError as e:
        return jsonify({"ok": False, "error": f"Couldn't save: {e}"}), 500
    APP_PASSWORD = new
    return jsonify({"ok": True})


# ─── Accounts manager ────────────────────────────────────────────────────────
# Docs & Settings → Accounts. Add / rename / activate-deactivate accounts
# without touching code. NO delete on purpose: transactions reference
# accounts by id, so removing one would orphan history — deactivating hides
# an account from the import dropdown and new-transaction pickers while
# every existing transaction keeps rendering.

# The types the app actually understands. checking / credit_card /
# digital_wallet each map to a CSV importer (Chase checking, Chase CC,
# Venmo formats); 'other' is manual-entry only.
ACCOUNT_TYPES = [
    ("checking",       "Checking"),
    ("credit_card",    "Credit card"),
    ("digital_wallet", "Digital wallet"),
    ("other",          "Other"),
]
_ACCOUNT_TYPE_KEYS = {k for k, _ in ACCOUNT_TYPES}


def _parse_billing_day(raw, label):
    """Parse a billing-day form field. Returns (value, error): '' → (None,
    None); non-integer or out of 1–31 → (None, <error string>)."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        day = int(raw)
    except ValueError:
        return None, f"{label} must be a whole number (1–31)."
    if not (1 <= day <= 31):
        return None, f"{label} must be between 1 and 31."
    return day, None


def _account_name_taken(db, name, exclude_id=None):
    """Case-insensitive duplicate-name check across ALL accounts (active or
    not) — two accounts with the same display name is a mis-import waiting
    to happen."""
    if exclude_id is None:
        row = db.execute(
            "SELECT id FROM accounts WHERE LOWER(name)=LOWER(?)", (name,)
        ).fetchone()
    else:
        row = db.execute(
            "SELECT id FROM accounts WHERE LOWER(name)=LOWER(?) AND id != ?",
            (name, exclude_id)
        ).fetchone()
    return row is not None


def settings_accounts():
    """Docs & Settings → Accounts. GET lists every row in the accounts
    table; POST handles add / rename / toggle actions (flash + redirect,
    same pattern as the import pages)."""
    db = get_db()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "add":
            name = (request.form.get("name") or "").strip()
            typ  = (request.form.get("type") or "").strip()
            num  = (request.form.get("account_num") or "").strip() or None
            close_day, close_err = _parse_billing_day(
                request.form.get("stmt_close_day"), "Statement close day")
            due_day, due_err = _parse_billing_day(
                request.form.get("pay_due_day"), "Payment due day")
            if not name:
                flash("Account name can't be empty.", "error")
            elif typ not in _ACCOUNT_TYPE_KEYS:
                flash("Pick a valid account type.", "error")
            elif _account_name_taken(db, name):
                flash(f'An account named "{name}" already exists.', "error")
            elif typ == "credit_card" and close_err:
                flash(close_err, "error")
            elif typ == "credit_card" and not close_day:
                # REQUIRED for credit cards: the close day drives statement-
                # date assignment on import and the Reconcile Card wizard.
                flash("Credit-card accounts need a statement close day "
                      "(1–31) — the day your statement cycle closes each "
                      "month.", "error")
            elif typ == "credit_card" and due_err:
                flash(due_err, "error")
            else:
                # Billing days only apply to credit cards; ignore any values
                # posted alongside other types.
                if typ != "credit_card":
                    close_day, due_day = None, None
                # Owner is always the app OWNER on insert (single-user build).
                db.execute(
                    "INSERT INTO accounts (name, account_num, type, owner, "
                    "active, stmt_close_day, pay_due_day) VALUES (?,?,?,?,1,?,?)",
                    (name, num, typ, OWNER, close_day, due_day)
                )
                if typ == "credit_card":
                    # Card-payment transfer category for this card (the
                    # canonical 'this transfer is a card payment' signal —
                    # L2 names the card). Mirrors the investments pattern.
                    db.execute(
                        """INSERT OR IGNORE INTO categories (trx_type, l1, l2)
                           VALUES ('transfer', 'Credit Card Payment', ?)""",
                        (name,))
                db.commit()
                flash(f'Account "{name}" added.', "success")

        elif action == "rename":
            account_id = request.form.get("account_id", type=int)
            name = (request.form.get("name") or "").strip()
            acct = db.execute("SELECT * FROM accounts WHERE id=?",
                              (account_id,)).fetchone() if account_id else None
            close_day, close_err = _parse_billing_day(
                request.form.get("stmt_close_day"), "Statement close day")
            due_day, due_err = _parse_billing_day(
                request.form.get("pay_due_day"), "Payment due day")
            is_cc = bool(acct) and acct["type"] == "credit_card"
            if not acct:
                flash("Unknown account.", "error")
            elif not name:
                flash("Account name can't be empty.", "error")
            elif _account_name_taken(db, name, exclude_id=account_id):
                flash(f'An account named "{name}" already exists.', "error")
            elif is_cc and (close_err or due_err):
                flash(close_err or due_err, "error")
            else:
                changes = []
                if name != acct["name"]:
                    db.execute("UPDATE accounts SET name=? WHERE id=?",
                               (name, account_id))
                    if is_cc:
                        # Keep the card-payment transfer convention intact
                        # (L2 == the card's name) — category row + already-
                        # categorized payment transfers follow the rename.
                        db.execute(
                            """UPDATE categories SET l2=? WHERE l2=?
                               AND trx_type='transfer'
                               AND l1='Credit Card Payment'""",
                            (name, acct["name"]))
                        db.execute(
                            """UPDATE transactions SET l2_category=?
                               WHERE l2_category=? AND trx_type='transfer'
                               AND l1_category='Credit Card Payment'""",
                            (name, acct["name"]))
                        db.execute(
                            """UPDATE staging SET l2_category=?
                               WHERE l2_category=? AND trx_type='transfer'
                               AND l1_category='Credit Card Payment'""",
                            (name, acct["name"]))
                    changes.append(f'Renamed "{acct["name"]}" to "{name}"')
                # Billing days — credit-card rows only. Blank inputs leave
                # the stored values as-is (so an existing card can be
                # renamed before its days are known), filled inputs update:
                # this is how existing cards add their close day.
                if is_cc:
                    if close_day and close_day != acct["stmt_close_day"]:
                        db.execute(
                            "UPDATE accounts SET stmt_close_day=? WHERE id=?",
                            (close_day, account_id))
                        changes.append(f"statement close day → {close_day}")
                    if due_day and due_day != acct["pay_due_day"]:
                        db.execute(
                            "UPDATE accounts SET pay_due_day=? WHERE id=?",
                            (due_day, account_id))
                        changes.append(f"payment due day → {due_day}")
                if changes:
                    db.commit()
                    flash(". ".join(changes) + ".", "success")

        elif action == "toggle":
            account_id = request.form.get("account_id", type=int)
            acct = db.execute("SELECT * FROM accounts WHERE id=?",
                              (account_id,)).fetchone() if account_id else None
            if not acct:
                flash("Unknown account.", "error")
            else:
                new_active = 0 if acct["active"] else 1
                db.execute("UPDATE accounts SET active=? WHERE id=?",
                           (new_active, account_id))
                db.commit()
                if new_active:
                    flash(f'Account "{acct["name"]}" reactivated.', "success")
                else:
                    flash(f'Account "{acct["name"]}" deactivated — hidden from '
                          "import and new-transaction pickers; its history stays "
                          "intact.", "success")

        else:
            flash("Unknown action.", "error")

        return redirect(url_for("settings_accounts"))

    accounts = db.execute("""
        SELECT a.*,
               (SELECT COUNT(*) FROM transactions t
                 WHERE t.account_id = a.id) AS trx_count
          FROM accounts a
         ORDER BY a.active DESC, a.name
    """).fetchall()
    return render_template("settings/accounts.html",
                           accounts=accounts,
                           account_types=ACCOUNT_TYPES)


# ─── Exchange rates ──────────────────────────────────────────────────────────

def settings_exchange_rates():
    """Docs & Settings → Assumptions → Exchange Rates. Rates the receipts
    pipeline's FX matcher uses (±10% band each). One rate per currency,
    one truth."""
    db = get_db()
    rates = [{"currency": c, "label": cfg["label"], "symbol": cfg["symbol"],
              "rate": get_fx_rate(db, c)} for c, cfg in FX_RATES.items()]
    return render_template("settings/exchange_rates.html", rates=rates)


def api_set_exchange_rate():
    """Body: {currency: 'EUR'|'GBP', per_usd: 0.92}. Sanity-bounded so a
    typo can't silently break FX matching."""
    data = request.get_json(silent=True) or {}
    currency = (data.get("currency") or "").upper()
    if currency not in FX_RATES:
        return jsonify({"ok": False, "error": f"unknown currency {currency}"}), 400
    try:
        rate = float(data.get("per_usd"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "numeric rate required"}), 400
    if not (0.1 <= rate <= 1000):
        return jsonify({"ok": False, "error": "rate out of sane bounds (0.1–1000)"}), 400
    db = get_db()
    set_setting(db, FX_RATES[currency]["key"], f"{rate:g}")
    db.commit()
    return jsonify({"ok": True, "currency": currency, "per_usd": rate})


# ─── Keyboard shortcuts ──────────────────────────────────────────────────────

def settings_shortcuts():
    """Docs & Settings → Shortcuts. ONE keymap for the whole app."""
    db = get_db()
    return render_template("settings/shortcuts.html",
                           shortcuts=get_shortcuts(db),
                           actions=SHORTCUT_ACTIONS)


def api_set_shortcuts():
    """Body: {confirm: 'Enter', skip: 's', ...}. Only known actions accepted;
    duplicate keys rejected (one key = one action, everywhere)."""
    import json as _json
    data = request.get_json(silent=True) or {}
    clean = {}
    for action, key in data.items():
        if action not in SHORTCUT_ACTIONS:
            continue
        key = (key or "").strip()
        if not key or len(key) > 20:
            return jsonify({"ok": False, "error": f"invalid key for {action}"}), 400
        clean[action] = key
    merged = dict(DEFAULT_SHORTCUTS); merged.update(clean)
    vals = [v.lower() for v in merged.values()]
    if len(set(vals)) != len(vals):
        return jsonify({"ok": False, "error": "duplicate key — each key can only trigger one action"}), 400
    db = get_db()
    set_setting(db, "keyboard_shortcuts", _json.dumps(merged))
    db.commit()
    return jsonify({"ok": True, "shortcuts": merged})


def settings_tools_menu():
    """Docs & Settings → Tools Menu. Check which tools appear in the Tools
    submenu; drag to reorder."""
    db = get_db()
    portal = OWNER
    order = get_tools_menu_visible(db, portal)   # saved list IS the order
    visible = set(order)
    # Checked-in-saved-order first, then the rest in registry order.
    offered = [t for t in TOOLS_REGISTRY if portal in t["portals"]]
    by_key = {t["key"]: t for t in offered}
    ordered = [by_key[k] for k in order if k in by_key]
    ordered += [t for t in offered if t["key"] not in visible]
    tools = [{"key": t["key"], "title": t["title"],
              "built": t["built"], "checked": t["key"] in visible}
             for t in ordered]
    return render_template("settings/tools_menu.html",
                           portal=portal, tools=tools)


def api_set_tools_menu():
    """Body: {keys: ['import', ...]}. Saves which tools show in the Tools
    submenu (and their order)."""
    import json as _json
    data = request.get_json(silent=True) or {}
    valid = {t["key"] for t in TOOLS_REGISTRY}
    keys = [k for k in (data.get("keys") or []) if k in valid]
    db = get_db()
    set_setting(db, "tools_menu_visible", _json.dumps(keys))
    db.commit()
    return jsonify({"ok": True, "portal": OWNER, "keys": keys})


def register(app, helpers):
    """Bind this module's routes. Endpoint names == function names,
    exactly as in the pre-split app.py."""
    login_required = helpers["login_required"]
    global settings_categorization, settings_overview, settings_assumptions, \
        settings_budget_values, api_budget_values_upsert, settings_security, \
        api_change_password, settings_exchange_rates, api_set_exchange_rate, \
        settings_shortcuts, api_set_shortcuts, settings_tools_menu, \
        api_set_tools_menu, api_categories_add, settings_accounts
    settings_categorization = login_required(settings_categorization)
    app.route("/settings/categorization")(settings_categorization)
    api_categories_add = login_required(api_categories_add)
    app.route("/api/settings/categories/add", methods=["POST"])(api_categories_add)
    settings_overview = login_required(settings_overview)
    app.route("/settings/overview")(settings_overview)
    settings_assumptions = login_required(settings_assumptions)
    app.route("/settings/assumptions")(settings_assumptions)
    settings_budget_values = login_required(settings_budget_values)
    app.route("/settings/assumptions/budget-values")(settings_budget_values)
    api_budget_values_upsert = login_required(api_budget_values_upsert)
    app.route("/api/budget-values/upsert", methods=["POST"])(api_budget_values_upsert)
    settings_accounts = login_required(settings_accounts)
    app.route("/settings/accounts", methods=["GET", "POST"])(settings_accounts)
    settings_security = login_required(settings_security)
    app.route("/settings/security")(settings_security)
    api_change_password = login_required(api_change_password)
    app.route("/api/settings/password", methods=["POST"])(api_change_password)
    settings_exchange_rates = login_required(settings_exchange_rates)
    app.route("/settings/assumptions/exchange-rates")(settings_exchange_rates)
    api_set_exchange_rate = login_required(api_set_exchange_rate)
    app.route("/api/settings/exchange-rate", methods=["POST"])(api_set_exchange_rate)
    settings_shortcuts = login_required(settings_shortcuts)
    app.route("/settings/shortcuts")(settings_shortcuts)
    api_set_shortcuts = login_required(api_set_shortcuts)
    app.route("/api/settings/shortcuts", methods=["POST"])(api_set_shortcuts)
    settings_tools_menu = login_required(settings_tools_menu)
    app.route("/settings/tools-menu")(settings_tools_menu)
    api_set_tools_menu = login_required(api_set_tools_menu)
    app.route("/api/settings/tools-menu", methods=["POST"])(api_set_tools_menu)
