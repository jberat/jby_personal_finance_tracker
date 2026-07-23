"""
routes_trx.py — transaction detail/update APIs, split/unsplit, links, tags,
reconcile, and search for Personal Financial Tracker (PFT).

No blueprints: register(app, helpers) binds every view under its original
function name, so endpoint names, url_for(...) and base.html `ep ==` checks
are unchanged.
"""
from flask import request, redirect, render_template, jsonify, flash
from db import get_db

# Cross-module helpers: imported from the module that owns them.
from routes_receipts import _build_receipt_family, _split_family_ids, _sync_family_receipts
from billing import calc_payment_dates

# ─── Reconcile API ────────────────────────────────────────────────────────────

def trx_reconcile(trx_id):
    db   = get_db()
    data = request.get_json(silent=True) or {}
    ext  = data.get("ext_ref", "").strip() or None
    db.execute("""
        UPDATE transactions
        SET reconciled_at = datetime('now'), ext_ref = ?
        WHERE id = ?
    """, (ext, trx_id))
    db.commit()
    return jsonify({"ok": True})

def trx_unreconcile(trx_id):
    db = get_db()
    db.execute(
        "UPDATE transactions SET reconciled_at=NULL, ext_ref=NULL WHERE id=?",
        (trx_id,)
    )
    db.commit()
    return jsonify({"ok": True})

# ─── Split Transaction API ────────────────────────────────────────────────────

def _has_links(db, trx_id) -> bool:
    """True if trx participates in the transaction_links M:M table.
    (2026-07-03 fix: guards previously checked the legacy transactions.link_id
    column, which the current Link UI never sets — so linked rows slipped
    through split guards.)"""
    return db.execute(
        "SELECT 1 FROM transaction_links WHERE a_id=? OR b_id=? LIMIT 1",
        (trx_id, trx_id)).fetchone() is not None


def trx_split(trx_id):
    """
    Body: { splits: [{amount, trx_type, l1_category, l2_category, note}, ...] }

    `amount` is a POSITIVE dollar magnitude; each child's stored sign is derived
    from its own trx_type so children can differ in type from the parent (e.g.
    split a -$2001 income/tax payment into $2000 income + $1 expense fee). The
    magnitudes must sum to the parent's magnitude. For same-type children this
    reproduces the prior signed behavior exactly.

    Sign convention (matches the importer/approve logic):
      - income / transfer  → stored = bank flow (money out negative)
      - expense            → stored = +outflow  (= -bank flow)
    """
    import hashlib
    db   = get_db()
    data = request.get_json(silent=True) or {}
    orig = db.execute("SELECT * FROM transactions WHERE id=?", (trx_id,)).fetchone()

    if not orig:
        return jsonify({"ok": False, "error": "Not found"}), 404

    # CC-recon guard (documented choice: BLOCK rather than migrate links):
    # a settled charge can't be split; unwind first.
    try:
        import routes_ccrecon as _ccr
        if _ccr.charge_is_settled(db, trx_id):
            return jsonify({"ok": False,
                            "error": "This charge is settled by a card "
                                     "payment — unwind that reconciliation "
                                     "first, split, then re-reconcile"}), 400
    except ImportError:
        pass
    # Guards (2026-07-03 fix — previously absent, so a deleted trx or a split
    # child could be split, corrupting family sums):
    if orig["status"] != "active":
        return jsonify({"ok": False, "error": "Transaction is not active"}), 400
    if orig["is_split"]:
        return jsonify({"ok": False, "error": "Already split"}), 400
    if orig["parent_id"] is not None:
        return jsonify({"ok": False, "error": "This is a split child — unsplit the parent first"}), 400

    splits = data.get("splits", [])
    if len(splits) < 2:
        return jsonify({"ok": False, "error": "Need at least 2 splits"}), 400

    NEUTRAL = ("income", "transfer")   # types that keep the raw bank sign
    parent_amt  = float(orig["amount"])
    parent_type = orig["trx_type"] or "expense"
    # Bank cash flow of the parent (− = money out). Children inherit this
    # direction; their stored sign then depends on each child's own type.
    parent_bank = parent_amt if parent_type in NEUTRAL else -parent_amt
    direction   = 1.0 if parent_bank >= 0 else -1.0

    total_mag = sum(abs(float(s.get("amount", 0))) for s in splits)
    if abs(total_mag - abs(parent_bank)) > 0.02:
        return jsonify({"ok": False, "error": f"Split amounts ({total_mag:.2f}) must equal original ({abs(parent_bank):.2f})"}), 400

    new_ids = []
    for i, s in enumerate(splits):
        mag      = abs(float(s["amount"]))
        typ      = s.get("trx_type") or parent_type
        owner    = orig["owner"]   # single portal — children always keep the owner
        # Derive stored sign from this child's type + the parent's cash direction.
        child_bank = direction * mag
        amt        = child_bank if typ in NEUTRAL else -child_bank
        l1 = s.get("l1_category") or None
        l2 = s.get("l2_category") or None
        # Baggage (2026-07-04): children inherit the parent's note
        # unless the split payload provides its own per-child note.
        note     = s.get("note") or orig["note"] or None
        raw      = f"{orig['raw_description']} [split {i+1}/{len(splits)}]"
        dk_raw   = f"{orig['trx_date']}|{raw.upper()}|{amt:.2f}"
        dk       = hashlib.md5(dk_raw.encode()).hexdigest()
        # Receipt carries over to all split children (still one receipt)
        receipt  = orig["receipt_path"] if orig["receipt_path"] else None

        cur = db.execute("""
            INSERT INTO transactions
              (account_id, parent_id, is_split,
               trx_date, post_date, statement_date, payment_date,
               raw_description, vendor, amount, trx_type, owner,
               l1_category, l2_category, note, receipt_path, dedup_key, status)
            VALUES (?,?,1, ?,?,?,?, ?,?,?,?,?, ?,?,?,?,?,'active')
        """, (
            orig["account_id"], trx_id,
            orig["trx_date"], orig["post_date"], orig["statement_date"], orig["payment_date"],
            raw, orig["vendor"], amt, typ, owner,
            l1, l2, note, receipt, dk,
        ))
        new_ids.append(cur.lastrowid)

    # Tags ride along to every child (2026-07-04: split baggage)
    for nid in new_ids:
        db.execute("""INSERT OR IGNORE INTO transaction_tags (trx_id, tag_id)
                      SELECT ?, tag_id FROM transaction_tags WHERE trx_id=?""",
                   (nid, trx_id))

    # Mark original as split + deleted
    db.execute(
        "UPDATE transactions SET is_split=1, status='deleted' WHERE id=?",
        (trx_id,)
    )
    # Sync receipt across the new split family. The inline copy above set
    # receipt_path on each new child from the original; this also syncs
    # any linked partners of the parent into the family.
    if new_ids:
        _sync_family_receipts(db, new_ids[0])
    db.commit()
    return jsonify({"ok": True, "new_ids": new_ids})


def trx_add_link(trx_id):
    """Add a many-to-many link between this trx and another. Idempotent —
    duplicate links are silently ignored. Returns the linked trx as a
    convenience for inline UI rendering."""
    db   = get_db()
    data = request.get_json(silent=True) or {}
    try:
        linked_id = int(data.get("linked_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid linked_id"}), 400
    if not linked_id or linked_id == trx_id:
        return jsonify({"ok": False, "error": "Pick a different transaction"}), 400

    other = db.execute("SELECT id, trx_date, vendor, amount, owner, trx_type "
                       "FROM transactions WHERE id=? AND status='active'",
                       (linked_id,)).fetchone()
    if not other:
        return jsonify({"ok": False, "error": "Linked transaction not found"}), 404

    a, b = sorted((trx_id, linked_id))
    db.execute(
        "INSERT OR IGNORE INTO transaction_links (a_id, b_id) VALUES (?, ?)",
        (a, b)
    )

    # Receipt propagation — symmetric, family-aware. Whichever side has a
    # receipt gets propagated to every member of the combined family
    # (split + linked, transitively). Common Amazon case: original purchase
    # has a receipt, the return doesn't — the link tells us they share one.
    _sync_family_receipts(db, trx_id)

    db.commit()
    return jsonify({"ok": True, "linked": dict(other)})


def trx_remove_link(trx_id, linked_id):
    """Remove a link between trx_id and linked_id (regardless of which side
    initiated it)."""
    db = get_db()
    a, b = sorted((trx_id, linked_id))
    db.execute(
        "DELETE FROM transaction_links WHERE a_id=? AND b_id=?", (a, b)
    )
    db.commit()
    return jsonify({"ok": True})


def trx_add_tag(trx_id):
    db   = get_db()
    name = (request.get_json(silent=True) or {}).get("name", "").strip().lower()
    if not name:
        return jsonify({"ok": False, "error": "Empty tag"}), 400
    # Upsert tag
    db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    tag = db.execute("SELECT * FROM tags WHERE name=?", (name,)).fetchone()
    # Link to transaction (ignore if already linked)
    db.execute(
        "INSERT OR IGNORE INTO transaction_tags (trx_id, tag_id) VALUES (?,?)",
        (trx_id, tag["id"])
    )
    db.commit()
    return jsonify({"ok": True, "tag": {"id": tag["id"], "name": tag["name"]}})

def trx_remove_tag(trx_id, tag_id):
    db = get_db()
    db.execute(
        "DELETE FROM transaction_tags WHERE trx_id=? AND tag_id=?",
        (trx_id, tag_id)
    )
    db.commit()
    return jsonify({"ok": True})

def trx_meta(trx_id):
    """Full editable state of a transaction — used by the receipt review screen
    as the single source of truth for the right-hand panel (account, category,
    note, tags) for whatever trx is the current match target."""
    db  = get_db()
    trx = db.execute("""
        SELECT t.id, t.vendor, t.raw_description, t.amount, t.trx_date,
               t.owner, t.trx_type, t.l1_category, t.l2_category, t.note,
               t.is_split, t.parent_id, a.name AS account_name
          FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
         WHERE t.id = ?
    """, (trx_id,)).fetchone()
    if not trx:
        return jsonify({"ok": False, "error": "not found"}), 404
    tags = db.execute("""
        SELECT t.id, t.name FROM tags t
          JOIN transaction_tags tt ON tt.tag_id = t.id
         WHERE tt.trx_id = ? ORDER BY t.name
    """, (trx_id,)).fetchall()
    d = dict(trx)
    d["tags"] = [dict(t) for t in tags]
    # Live duplicate signal (2026-07-04): the reviewer re-evaluates its
    # duplicate banner whenever the target trx changes (repick), so it needs
    # to know if THIS trx already has a filed receipt — same rule as the
    # review-page query in routes_receipts.py.
    d["has_receipt"] = db.execute(
        "SELECT 1 FROM receipts WHERE matched_trx_id=? AND status='filed' LIMIT 1",
        (trx_id,)).fetchone() is not None
    d["ok"] = True
    return jsonify(d)


def trx_unsplit(trx_id):
    """Reverse a split: delete child rows, restore the parent."""
    db = get_db()
    parent = db.execute("SELECT * FROM transactions WHERE id=?", (trx_id,)).fetchone()
    if not parent:
        return jsonify({"ok": False, "error": "Not found"}), 404
    # Guard (2026-07-03 fix — previously none, so unsplit could run on any id):
    if not parent["is_split"]:
        return jsonify({"ok": False, "error": "Not a split transaction"}), 400
    # Delete all child rows that reference this parent
    db.execute("UPDATE transactions SET status='deleted' WHERE parent_id=?", (trx_id,))
    # Restore parent
    db.execute(
        "UPDATE transactions SET status='active', is_split=0 WHERE id=?",
        (trx_id,)
    )
    db.commit()
    return jsonify({"ok": True})

# ─── Transaction Detail ───────────────────────────────────────────────────────

def trx_detail(trx_id):
    db  = get_db()
    trx = db.execute("""
        SELECT t.*, a.name as account_name, a.type as account_type
        FROM transactions t JOIN accounts a ON t.account_id = a.id
        WHERE t.id = ?
    """, (trx_id,)).fetchone()

    if not trx:
        flash("Transaction not found.", "error")
        return redirect("/expenses/transactions")

    # Categories for dropdowns
    categories = db.execute(
        "SELECT * FROM categories ORDER BY trx_type, l1, l2"
    ).fetchall()
    cat_map = {}
    for c in categories:
        key = f"{c['trx_type']}:{c['l1']}"
        cat_map.setdefault(key, []).append(c["l2"])

    accounts = db.execute("SELECT * FROM accounts WHERE active=1 ORDER BY name").fetchall()

    # Same-vendor transactions (last 10, excluding this one).
    vendor_key = trx["vendor"] or trx["raw_description"]
    similar = db.execute("""
        SELECT t.*, a.name as account_name
        FROM transactions t JOIN accounts a ON t.account_id = a.id
        WHERE (t.vendor = ? OR (t.vendor IS NULL AND t.raw_description = ?))
          AND t.id != ? AND t.status = 'active'
        ORDER BY t.trx_date DESC LIMIT 10
    """, (vendor_key, vendor_key, trx_id)).fetchall()

    trx_tags = db.execute("""
        SELECT t.* FROM tags t
        JOIN transaction_tags tt ON tt.tag_id = t.id
        WHERE tt.trx_id = ?
        ORDER BY t.name
    """, (trx_id,)).fetchall()

    # Flat L1→[L2] map for this trx's type ('expense' / 'income' / 'transfer')
    target_cat_type = trx["trx_type"]
    l1_l2_map = {}
    for c in categories:
        if c["trx_type"] == target_cat_type:
            l1_l2_map.setdefault(c["l1"], []).append(c["l2"])

    # Import batch info (if this trx came from a CSV import)
    import_batch = None
    if trx["staging_id"]:
        import_batch = db.execute("""
            SELECT b.filename, b.imported_at, b.row_count, b.id as batch_id
            FROM import_batches b
            JOIN staging s ON s.import_batch_id = b.id
            WHERE s.id = ?
        """, (trx["staging_id"],)).fetchone()

    # Linked transactions — pulled from the symmetric transaction_links
    # join table (M:M). One trx can have any number of links.
    linked = db.execute("""
        SELECT t.id, t.trx_date, t.vendor, t.raw_description, t.amount,
               t.trx_type, t.owner, t.l1_category, t.l2_category, a.name as account_name
        FROM transactions t JOIN accounts a ON t.account_id=a.id
        WHERE t.status='active'
          AND t.id IN (
              SELECT b_id FROM transaction_links WHERE a_id=?
              UNION
              SELECT a_id FROM transaction_links WHERE b_id=?
          )
        ORDER BY t.trx_date ASC, t.id ASC
    """, (trx_id, trx_id)).fetchall()

    # Multi-receipt: pull every receipts row tied to this trx OR any of its
    # split-family + linked-family relatives (so a child sees the parent's
    # receipt, siblings see each other's, linked partners share). The detail
    # page lists all of them; trx-table list views read the denormalized
    # transactions.receipt_path (= primary, lowest-id receipt).
    #
    # Dedupe by filed_path (lowest-id row wins) — when a user adds a
    # receipt to a child after a split, both the parent's receipts row
    # and the child's would point to the same file; the family walk would
    # otherwise show it twice.
    family = list(_build_receipt_family(db, trx_id))
    placeholders = ",".join("?" * len(family))
    receipts = db.execute(f"""
        WITH fam AS (
            SELECT id, filename, filed_path, file_type, extracted_vendor,
                   extracted_amount, extracted_date, extractor_used, created_at,
                   ROW_NUMBER() OVER (PARTITION BY filed_path ORDER BY id) AS rn
              FROM receipts
             WHERE matched_trx_id IN ({placeholders}) AND status='filed'
        )
        SELECT id, filename, filed_path, file_type, extracted_vendor,
               extracted_amount, extracted_date, extractor_used, created_at
          FROM fam WHERE rn = 1
         ORDER BY id ASC
    """, family).fetchall()

    return render_template("trx_detail.html",
        trx=trx,
        cat_map=cat_map,
        l1_l2_map=l1_l2_map,
        accounts=accounts,
        similar=similar,
        trx_tags=trx_tags,
        import_batch=import_batch,
        linked=linked,
        receipts=receipts,
    )


# ─── Calc statement date helper (for review queue JS) ────────────────────────

def api_calc_stmt_date():
    post_date  = request.args.get("post_date", "")
    account_id = request.args.get("account_id", "")
    if not post_date or not account_id:
        return jsonify({"statement_date": None, "payment_date": None})
    db = get_db()
    acct = db.execute("SELECT account_num FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not acct:
        return jsonify({"statement_date": None, "payment_date": None})
    stmt, pay = calc_payment_dates(db, post_date, acct["account_num"])
    return jsonify({"statement_date": stmt, "payment_date": pay})


# ─── Transactions API (inline edit) ───────────────────────────────────────────

def trx_search():
    """
    Lightweight search for the link-transaction picker.
    Matches `q` as a case-insensitive substring against vendor and
    raw_description. Optional `exclude_id` keeps the source trx out
    of its own results. Returns up to 25 most-recent matches.
    """
    db = get_db()
    q  = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    exclude_id = request.args.get("exclude_id", type=int)

    pattern = f"%{q}%"
    sql = """
        SELECT t.id, t.trx_date, t.vendor, t.raw_description, t.amount,
               t.trx_type, t.owner,
               EXISTS(SELECT 1 FROM transaction_links tl
                      WHERE tl.a_id = t.id OR tl.b_id = t.id) AS has_link
        FROM transactions t
        WHERE t.status='active'
          AND (t.vendor LIKE ? OR t.raw_description LIKE ? OR t.note LIKE ?)
    """
    params = [pattern, pattern, pattern]
    if exclude_id:
        sql += " AND t.id != ?"
        params.append(exclude_id)
    sql += " ORDER BY t.trx_date DESC, t.id DESC LIMIT 25"

    rows = db.execute(sql, params).fetchall()
    return jsonify({"results": [dict(r) for r in rows]})


def trx_update(trx_id):
    db   = get_db()
    data = request.get_json(silent=True) or {}

    # In-place update. Single portal: owner never changes, so it's not an
    # editable field here.
    allowed = {"vendor", "trx_date", "post_date", "amount", "trx_type",
               "l1_category", "l2_category", "note", "receipt_path",
               "account_id", "statement_date", "payment_date", "link_id",
               "no_receipt_needed"}
    # Numeric fields must stay numeric: SQLite would happily store a TEXT
    # amount (dynamic typing), which then breaks every page that does math
    # on it (e.g. abs() in the Reconcile Card landing view) — an
    # undiagnosable 500 far from the cause. Reject garbage here instead.
    _numeric = {"amount": float, "account_id": int,
                "link_id": int, "no_receipt_needed": int}
    for k, cast in _numeric.items():
        if k in data and data[k] not in ("", None):
            try:
                data[k] = cast(data[k])
            except (TypeError, ValueError):
                return jsonify({"ok": False,
                                "error": f"{k} must be a number"}), 400
    sets, vals = [], []
    for k, v in data.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v if v != "" else None)
    if not sets:
        return jsonify({"ok": True})
    vals.append(trx_id)
    db.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=?", vals)

    # Receipt path propagates across the split family — every sibling +
    # the (deleted) parent share one receipt. Only fires when the caller
    # is actually setting receipt_path.
    if "receipt_path" in data:
        family = _split_family_ids(db, trx_id)
        if len(family) > 1:
            placeholders = ",".join("?" * len(family))
            new_val = data["receipt_path"] if data["receipt_path"] else None
            db.execute(
                f"UPDATE transactions SET receipt_path=? WHERE id IN ({placeholders})",
                [new_val, *family]
            )
        # (2026-07-04) Adopted a real receipt → the no-receipt pass is
        # moot; clear it (family-wide) so detaching later re-surfaces the row.
        if data["receipt_path"]:
            placeholders = ",".join("?" * len(family))
            db.execute(
                f"UPDATE transactions SET no_receipt_needed=0 WHERE id IN ({placeholders})",
                list(family)
            )

    db.commit()
    return jsonify({"ok": True})

def trx_delete(trx_id):
    db = get_db()
    # Guard (2026-07-03 fix): deleting a split child silently breaks the
    # family's sum-to-parent invariant; it must go through Undo Split.
    row = db.execute("SELECT parent_id, is_split, status FROM transactions WHERE id=?",
                     (trx_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if row["parent_id"] is not None and row["status"] == "active":
        return jsonify({"ok": False, "error": "This is a split child — use Undo Split on the parent, then delete"}), 400
    # CC-recon hook: trashing a settled charge or a reconciled card payment
    # auto-unwinds the affected settlement so the cycle can be re-reconciled
    # honestly.
    try:
        import routes_ccrecon as _ccr
        _ccr.auto_unwind_for_trx(db, trx_id)
    except Exception:
        pass
    db.execute("UPDATE transactions SET status='deleted' WHERE id=?", (trx_id,))
    # If this was a synced investment transfer, pull its lot event back out so
    # the engine stays consistent (safe for pristine contributions only).
    import routes_investments as _inv
    _inv.auto_unsync_trx(db, trx_id)
    db.commit()
    return jsonify({"ok": True})


def register(app, helpers):
    """Bind this module's routes. Endpoint names == function names,
    exactly as in the pre-split app.py."""
    login_required = helpers["login_required"]
    global trx_reconcile, trx_unreconcile, trx_split, \
        trx_add_link, trx_remove_link, trx_add_tag, trx_remove_tag, trx_meta, \
        trx_unsplit, trx_detail, api_calc_stmt_date, trx_search, trx_update, \
        trx_delete, amt
    amt = helpers["amt"]
    trx_reconcile = login_required(trx_reconcile)
    app.route("/api/transactions/<int:trx_id>/reconcile", methods=["POST"])(trx_reconcile)
    trx_unreconcile = login_required(trx_unreconcile)
    app.route("/api/transactions/<int:trx_id>/unreconcile", methods=["POST"])(trx_unreconcile)
    trx_split = login_required(trx_split)
    app.route("/api/transactions/<int:trx_id>/split", methods=["POST"])(trx_split)
    trx_add_link = login_required(trx_add_link)
    app.route("/api/transactions/<int:trx_id>/links", methods=["POST"])(trx_add_link)
    trx_remove_link = login_required(trx_remove_link)
    app.route("/api/transactions/<int:trx_id>/links/<int:linked_id>", methods=["DELETE"])(trx_remove_link)
    trx_add_tag = login_required(trx_add_tag)
    app.route("/api/transactions/<int:trx_id>/tags", methods=["POST"])(trx_add_tag)
    trx_remove_tag = login_required(trx_remove_tag)
    app.route("/api/transactions/<int:trx_id>/tags/<int:tag_id>", methods=["DELETE"])(trx_remove_tag)
    trx_meta = login_required(trx_meta)
    app.route("/api/transactions/<int:trx_id>/meta", methods=["GET"])(trx_meta)
    trx_unsplit = login_required(trx_unsplit)
    app.route("/api/transactions/<int:trx_id>/unsplit", methods=["POST"])(trx_unsplit)
    trx_detail = login_required(trx_detail)
    app.route("/transactions/<int:trx_id>")(trx_detail)
    api_calc_stmt_date = login_required(api_calc_stmt_date)
    app.route("/api/calc-stmt-date")(api_calc_stmt_date)
    trx_search = login_required(trx_search)
    app.route("/api/transactions/search")(trx_search)
    trx_update = login_required(trx_update)
    app.route("/api/transactions/<int:trx_id>", methods=["POST"])(trx_update)
    trx_delete = login_required(trx_delete)
    app.route("/api/transactions/<int:trx_id>/delete", methods=["POST"])(trx_delete)
