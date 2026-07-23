"""
routes_receipts.py — receipts pipeline pages + APIs (orphans/review/trash), filing, preview, scan-inbox.

No blueprints: register(app, helpers) binds every view under its original
function name, so endpoint names, url_for(...) and base.html `ep ==` checks
are unchanged.
"""
import os
from flask import (request, redirect, render_template, jsonify, send_file)
from config import (
    BASE_DIR, OWNER, RECEIPTS_INBOX, RECEIPTS_ROOT, to_mac_path,
)
from db import get_db
from receipts_engine import (
    canonical_filed_path_for_trx as _canonical_filed_path,
    move_with_collision_suffix,
)

# Cross-module helpers: imported from the module that owns them.
from routes_settings import get_shortcuts

# Trashed receipt files (discarded duplicates, not-a-receipt files) live here
# until the user restores or permanently deletes them.
RECEIPTS_TRASH = os.path.join(RECEIPTS_ROOT, "trash")

# ─── Receipts (multi-receipt support — see docs/handbook.html §7) ────────────

def _build_receipt_family(db, trx_id):
    """Returns the set of trx ids that should share a receipt with trx_id:
       - trx_id itself
       - everyone in trx_id's split family (parent + siblings, recursively)
       - every trx linked to anyone in the family (transitive closure)

    Used by _sync_family_receipts() to propagate receipt_path correctly
    when a receipt is added/removed/moved on any member of the family.
    """
    family  = {trx_id}
    frontier = {trx_id}
    while frontier:
        new_frontier = set()
        for tid in frontier:
            # Split-family relatives
            new_frontier |= set(_split_family_ids(db, tid)) - family
            # Linked relatives (symmetric)
            linked = db.execute("""
                SELECT b_id AS other FROM transaction_links WHERE a_id=?
                UNION
                SELECT a_id AS other FROM transaction_links WHERE b_id=?
            """, (tid, tid)).fetchall()
            new_frontier |= ({r["other"] for r in linked}) - family
        family |= new_frontier
        frontier = new_frontier
    return family


def _sync_family_receipts(db, trx_id):
    """Sync receipt_path across trx_id's full receipt family (split + linked).

    Two passes:
      1. Per-trx primary: each member's receipt_path is set to that trx's
         own lowest-id filed receipt's filed_path (if any).
      2. Family fill: any member still NULL adopts the canonical path from
         a member that has one. Same physical file shared across the
         family — no copies needed.

    All paths normalized via to_mac_path().
    """
    family = _build_receipt_family(db, trx_id)

    # Pass 1: per-trx primary
    for tid in family:
        row = db.execute("""
            SELECT filed_path FROM receipts
             WHERE matched_trx_id=? AND status='filed' AND filed_path IS NOT NULL
             ORDER BY id ASC LIMIT 1
        """, (tid,)).fetchone()
        primary = to_mac_path(row["filed_path"]) if row and row["filed_path"] else None
        db.execute("UPDATE transactions SET receipt_path=? WHERE id=?",
                   (primary, tid))

    # Pass 2: family fill — find any non-null receipt_path, copy to NULLs
    canonical = None
    for tid in family:
        rp = db.execute("SELECT receipt_path FROM transactions WHERE id=?",
                        (tid,)).fetchone()
        if rp and rp["receipt_path"]:
            canonical = rp["receipt_path"]
            break
    if canonical:
        ph = ",".join("?" * len(family))
        db.execute(f"""
            UPDATE transactions SET receipt_path=?
             WHERE id IN ({ph}) AND receipt_path IS NULL
        """, [canonical, *family])

    # A row that ADOPTED a real receipt (via link, split, confirm,
    # reroute...) no longer needs its "no receipt needed" pass — clear the
    # flag so if the receipt is ever detached, the row goes back to being
    # tracked by Missing Receipts.
    ph = ",".join("?" * len(family))
    db.execute(f"""
        UPDATE transactions SET no_receipt_needed=0
         WHERE id IN ({ph}) AND receipt_path IS NOT NULL
           AND COALESCE(no_receipt_needed,0)=1
    """, list(family))


# Back-compat alias — every existing caller of _sync_primary_receipt now
# gets the family-aware behavior automatically. Single trx is just a
# family of one.
_sync_primary_receipt = _sync_family_receipts


# _canonical_filed_path / move_with_collision_suffix live in
# receipts_engine.py — imported above under their local names.


def _file_receipt(db, receipt_id: int, trx_id: int):
    """Move a receipt's underlying file from its current location to the
    canonical destination folder (receipts/filed/<YYYY>/<L1>/<L2>/<MM>/),
    renaming to YYMMDD Vendor.<ext>. Updates receipts.filed_path. Caller
    must call _sync_primary_receipt(trx_id) afterward and commit. Raises
    FileNotFoundError if the source can't be located in either filed_path
    or the inbox.

    This is the function the review-confirm + orphan-match endpoints call,
    so the confirm-button click actually moves files.
    """
    receipt = db.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    trx     = db.execute("SELECT * FROM transactions WHERE id=?", (trx_id,)).fetchone()
    if not receipt:
        raise ValueError(f"receipt {receipt_id} not found")
    if not trx:
        raise ValueError(f"transaction {trx_id} not found")

    # Locate the source file. Try filed_path first, then inbox.
    src = None
    candidates = []
    if receipt["filed_path"]:
        candidates.append(receipt["filed_path"])
    candidates.append(os.path.join(RECEIPTS_INBOX, receipt["filename"]))
    for c in candidates:
        if c and os.path.isfile(c):
            src = c; break
    if not src:
        raise FileNotFoundError(
            f"can't locate file for receipt {receipt_id} "
            f"(tried: {candidates})"
        )

    ext = os.path.splitext(receipt["filename"])[1].lower() or ".pdf"
    # Collision " (n)" counter + src==dest idempotence live in
    # receipts_engine. str() because this function's callers work in
    # strings/os.path.
    dest = str(move_with_collision_suffix(src, _canonical_filed_path(trx, ext)))

    db.execute("UPDATE receipts SET filed_path=?, status='filed', "
               "updated_at=datetime('now') WHERE id=?",
               (to_mac_path(dest), receipt_id))
    # Return src too so callers can move the file back if a later step in
    # their transaction fails (keeps disk consistent with the DB rollback).
    return dest, src


def _is_safe_receipt_path(path: str) -> bool:
    """True only if path resolves inside the receipts tree (inbox, filed,
    or trash). Blocks path traversal / arbitrary file reads via preview +
    add-receipt endpoints."""
    if not path:
        return False
    real = os.path.realpath(path)
    r = os.path.realpath(RECEIPTS_ROOT)
    return real == r or real.startswith(r + os.sep)


def receipt_preview():
    """Serve a receipt file for inline preview in new tab.
    Only serves files inside the receipts tree — never arbitrary paths."""
    path = request.args.get("path", "")
    if not _is_safe_receipt_path(path):
        return "Receipt path not allowed.", 403
    if not os.path.isfile(path):
        return "Receipt file not found.", 404
    return send_file(path, as_attachment=False)


def trx_receipt_add(trx_id):
    """Add a receipt to a transaction. Body: {path: "..."}.

    Creates a new receipts row with status='filed', extractor_used='manual'.
    Syncs transactions.receipt_path (denormalized primary) to the lowest-id
    remaining receipt afterward. Idempotent on the same path — if a receipt
    with this path already exists for this trx, returns it without dup.
    """
    db   = get_db()
    data = request.get_json(silent=True) or {}
    path = to_mac_path((data.get("path") or "").strip())
    if not path:
        return jsonify({"ok": False, "error": "path required"}), 400
    if not _is_safe_receipt_path(path):
        return jsonify({"ok": False,
                        "error": "path must be inside the receipts folder"}), 400

    trx = db.execute("SELECT id, vendor, amount, trx_date FROM transactions WHERE id=?",
                     (trx_id,)).fetchone()
    if not trx:
        return jsonify({"ok": False, "error": "transaction not found"}), 404

    # Idempotency: if this exact path is already filed against THIS trx OR
    # against any family member (split parent / sibling / linked partner),
    # don't create a duplicate row. The family-aware receipts query in
    # trx_detail already shows it on every family member's detail page —
    # the goal here is only to avoid stacking duplicate rows in the DB.
    family = list(_build_receipt_family(db, trx_id))
    placeholders = ",".join("?" * len(family))
    existing = db.execute(f"""
        SELECT id, matched_trx_id FROM receipts
         WHERE matched_trx_id IN ({placeholders})
           AND filed_path=? AND status='filed'
         ORDER BY id ASC LIMIT 1
    """, family + [path]).fetchone()
    if existing:
        return jsonify({
            "ok": True,
            "receipt_id": existing["id"],
            "duplicate": True,
            "family_owner_trx_id": existing["matched_trx_id"],
        })

    # Detect file_type from extension
    p = path.lower()
    if   p.endswith(".pdf"):  ftype = "application/pdf"
    elif p.endswith(".png"):  ftype = "image/png"
    elif p.endswith((".jpg", ".jpeg")): ftype = "image/jpeg"
    elif p.endswith(".heic"): ftype = "image/heic"
    else: ftype = None

    cur = db.execute("""
        INSERT INTO receipts
            (filename, file_type, inbox_seen_at,
             extracted_vendor, extracted_amount, extracted_date,
             extractor_used, extraction_confidence,
             matched_trx_id, match_confidence, status, filed_path, owner)
        VALUES (?, ?, datetime('now'), ?, ?, ?, 'manual', 1.0, ?, 'HIGH', 'filed', ?, ?)
    """, (
        path.split("/")[-1] if "/" in path else path,
        ftype,
        trx["vendor"], trx["amount"], trx["trx_date"],
        trx_id, path, OWNER,
    ))
    new_id = cur.lastrowid

    _sync_primary_receipt(db, trx_id)
    db.commit()
    return jsonify({"ok": True, "receipt_id": new_id})


def receipt_delete(receipt_id):
    """Remove a receipt from a transaction (does NOT delete the file on disk —
    just removes the DB row + re-syncs primary). The file stays in its filed
    location so the user can manually delete or re-attach."""
    db = get_db()
    row = db.execute(
        "SELECT id, matched_trx_id FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "receipt not found"}), 404

    trx_id = row["matched_trx_id"]
    db.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))
    if trx_id:
        _sync_primary_receipt(db, trx_id)
    db.commit()
    return jsonify({"ok": True})


# ─── Receipts dashboard pages ────────────────────────────────────────────────
# Views over the receipts table (see receipts_pipeline.md). Each pulls a
# different status slice. The CLI process_receipts.py is the deterministic
# fill engine; these pages let the user see + act on what the CLI flagged.


def _receipts_owner_filter() -> str:
    """SQL fragment for the owner filter — receipts stamped with the app
    owner, PLUS un-stamped orphans (legacy rows that never got an owner)."""
    return f"(r.owner = '{OWNER}' OR (r.owner IS NULL AND r.status='orphan'))"


# Receipts scope: Review + Orphans.
# Review:  pipeline-matched rows awaiting Confirm/Reject, plus orphans.
# Orphans: unmatched rows where the user picks a trx manually.
#
# Pages removed (intentionally simpler):
#   - Overview dashboard  (counts available via DB queries when needed)
#   - Linked              (status='linked' is transient bookkeeping)
#   - Missing             (replaced by the "No receipt" filter button on
#                          the expense tables)
#   - Inbox               (it's just the local receipts/inbox/ folder)


def _attach_preview_path(rows):
    """For each receipt row, compute the file's current location for inline
    preview: filed_path if filed, else INBOX/<filename>. Returns list of
    dicts (since sqlite3.Row is immutable)."""
    out = []
    for r in rows:
        d = dict(r)
        d["preview_path"] = d.get("filed_path") or os.path.join(
            RECEIPTS_INBOX, d["filename"]
        )
        out.append(d)
    return out


# /receipts/queue REMOVED — fully replaced by the one-at-a-time
# /receipts/review screen. Orphans page stays: it's the "couldn't match
# anything" inbox, with vendor/amount (±10%) search that files immediately
# via confirm-file. Old queue URLs redirect to the reviewer.
def receipts_queue():
    return redirect("/receipts/review")


def receipts_orphans():
    """Orphans: SAME one-at-a-time review experience as /receipts/review —
    big image, shared shortcuts, vendor/amount(±10%) search with has-receipt
    flags — just filtered to receipts that couldn't be matched to any
    transaction."""
    return _receipts_review_page(("orphan",), mode="orphans")


def receipts_review():
    """One-at-a-time reviewer over everything pending — queued matches (with
    a suggested trx) AND orphans (need a manual pick). Big receipt preview
    beside the transaction; confirm/recategorize/split/repick inline, then
    advance. Confirm files immediately (move + rename + receipt_path via
    /confirm-file); keyboard shortcuts come from the shared keymap in
    Docs & Settings → Shortcuts."""
    return _receipts_review_page(("queued", "orphan", "suspected_duplicate"),
                                 mode="all")


def _receipts_review_page(statuses, mode):
    db     = get_db()
    portal = OWNER
    status_sql = ",".join(f"'{s}'" for s in statuses)
    rows = db.execute(f"""
        SELECT r.*, t.id AS trx_id, t.trx_date AS trx_date,
               t.vendor AS trx_vendor, t.amount AS trx_amount,
               t.owner AS trx_owner, t.trx_type AS trx_type,
               t.l1_category AS trx_l1, t.l2_category AS trx_l2,
               t.raw_description AS trx_raw,
               EXISTS(SELECT 1 FROM receipts r2
                       WHERE r2.matched_trx_id = t.id AND r2.id != r.id
                         AND r2.status = 'filed') AS trx_has_receipt
          FROM receipts r
          LEFT JOIN transactions t ON t.id = r.matched_trx_id
         WHERE r.status IN ({status_sql})
           AND {_receipts_owner_filter()}
         ORDER BY (CASE r.status WHEN 'queued' THEN 0
                                 WHEN 'orphan' THEN 1 ELSE 2 END), r.id
    """).fetchall()

    items = []
    for r in _attach_preview_path(rows):
        items.append({
            "id":               r["id"],
            "filename":         r.get("filename"),
            "preview_path":     r["preview_path"],
            "ext":              (r.get("filename") or "").rsplit(".", 1)[-1].lower(),
            "status":           r["status"],
            "extracted_vendor": r.get("extracted_vendor"),
            "extracted_amount": r.get("extracted_amount"),
            "extracted_date":   r.get("extracted_date"),
            "notes":            r.get("notes"),
            # Duplicate signals: pipeline-flagged dup rows AND any queued
            # match whose trx already has a filed receipt get a banner in
            # the reviewer.
            "trx_has_receipt":  bool(r.get("trx_has_receipt")),
            # Panel pulls full trx detail (cat/account/note/tags) from
            # /api/transactions/<id>/meta; we just need the id to start.
            "match_trx_id":     r.get("trx_id"),
        })

    # Category map: cat_type -> { l1: [l2, ...] } for inline recategorize + split
    cat_map = {}
    for c in db.execute("SELECT trx_type,l1,l2 FROM categories ORDER BY trx_type,l1,l2"):
        cat_map.setdefault(c["trx_type"], {}).setdefault(c["l1"], []).append(c["l2"])

    return render_template("receipts/review.html",
        items=items, portal=portal, cat_map=cat_map,
        shortcuts=get_shortcuts(db), mode=mode)


# The legacy LINK-ONLY endpoints (/confirm, /match) are gone — they parked
# receipts in an invisible 'linked' limbo awaiting a batch filing step that
# no longer exists in the workflow. The reviewer AND the orphans page both
# go through /confirm-file, which files immediately. Any residual 'linked'
# rows are still picked up by process_receipts.py's file_pending on the
# next scan.


def receipts_queue_reject(receipt_id):
    """Reject a suggested match. Sets status='orphan' and clears
    matched_trx_id so the receipt surfaces in the orphans view."""
    db = get_db()
    if not db.execute("SELECT 1 FROM receipts WHERE id=?",
                      (receipt_id,)).fetchone():
        return jsonify({"ok": False, "error": "Receipt not found"}), 404
    db.execute("""
        UPDATE receipts
           SET status='orphan', matched_trx_id=NULL, match_confidence='NONE',
               updated_at=datetime('now')
         WHERE id=?
    """, (receipt_id,))
    db.commit()
    return jsonify({"ok": True})


def receipts_confirm_file(receipt_id):
    """Review-screen confirm: set/confirm the match (optional body {trx_id}),
    then FILE the receipt immediately — move the file to its canonical folder
    (creating dirs as needed), set receipts.filed_path + status='filed', and
    sync transactions.receipt_path (+ split/linked family). Self-contained:
    the Flask app runs locally, so it does all filesystem work right here —
    no CLI step."""
    db   = get_db()
    data = request.get_json(silent=True) or {}
    r = db.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "Receipt not found"}), 404
    trx_id = data.get("trx_id") or r["matched_trx_id"]
    if not trx_id:
        return jsonify({"ok": False, "error": "No transaction to file against"}), 400
    trx = db.execute("SELECT owner, status, is_split FROM transactions WHERE id=?",
                     (trx_id,)).fetchone()
    if not trx:
        return jsonify({"ok": False, "error": "Transaction not found"}), 404
    # Same target rule as orphan-match: active rows or deleted split parents
    # (receipt then propagates to all children).
    if trx["status"] != "active" and not (trx["status"] == "deleted" and trx["is_split"]):
        return jsonify({"ok": False, "error": "Transaction is deleted — pick an active one (or its split parent)"}), 400
    # Confirm/refresh the link + owner, then move the file.
    db.execute("""UPDATE receipts SET matched_trx_id=?, owner=?,
                     match_confidence='HIGH', status='linked',
                     updated_at=datetime('now')
                   WHERE id=?""", (trx_id, OWNER, receipt_id))
    try:
        dest, src = _file_receipt(db, receipt_id, trx_id)
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": f"File move failed: {e}"}), 500
    # If anything after the physical move fails, move the file back before
    # rolling back — otherwise the DB reverts but the file stays relocated,
    # and the receipt row points at a path that's now empty.
    try:
        _sync_primary_receipt(db, trx_id)
        db.commit()
    except Exception as e:
        import shutil as _shutil
        try:
            if dest != src and os.path.isfile(dest) and not os.path.exists(src):
                _shutil.move(dest, src)
        except OSError:
            pass  # file stays at dest; the retry path re-locates via filed_path
        db.rollback()
        return jsonify({"ok": False, "error": f"Save failed (file restored): {e}"}), 500
    return jsonify({"ok": True, "status": "filed", "filed_path": dest})


def receipts_discard(receipt_id):
    """Discard a duplicate receipt: move the physical file to receipts/trash/
    (never hard-delete a file) and mark the row status='discarded' so it
    disappears from every review surface. Used by the reviewer's duplicate
    banner."""
    import shutil as _shutil
    db = get_db()
    r = db.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "Receipt not found"}), 404
    if r["status"] == "filed":
        return jsonify({"ok": False, "error": "Refusing to discard a FILED receipt — remove it from the transaction first"}), 400

    # Locate the physical file: filed_path first, then inbox by filename.
    src = None
    for cand in (r["filed_path"],
                 os.path.join(RECEIPTS_INBOX, r["filename"] or "")):
        if cand and os.path.isfile(cand):
            src = cand
            break
    trashed_to = None
    if src:
        os.makedirs(RECEIPTS_TRASH, exist_ok=True)
        dest = os.path.join(RECEIPTS_TRASH, os.path.basename(src))
        n = 2
        while os.path.exists(dest):
            stem, ext = os.path.splitext(os.path.basename(src))
            dest = os.path.join(RECEIPTS_TRASH, f"{stem} ({n}){ext}")
            n += 1
        try:
            _shutil.move(src, dest)
            trashed_to = to_mac_path(dest)
        except OSError as e:
            return jsonify({"ok": False, "error": f"Couldn't move file to trash: {e}"}), 500

    db.execute("""UPDATE receipts SET status='discarded',
                     notes=COALESCE(notes,'') || ' [discarded as duplicate]',
                     updated_at=datetime('now') WHERE id=?""", (receipt_id,))
    db.commit()
    return jsonify({"ok": True, "trashed_to": trashed_to})


def receipts_not_receipt(receipt_id):
    """Mark a file as not-a-receipt: move it to receipts/trash/ (recoverable
    until trash is emptied) and set status='not_receipt' so it leaves every
    review surface."""
    import shutil as _shutil
    db = get_db()
    r = db.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "Receipt not found"}), 404
    if r["status"] == "filed":
        return jsonify({"ok": False, "error": "This receipt is FILED — remove it from its transaction first"}), 400
    src = None
    for cand in (r["filed_path"],
                 os.path.join(RECEIPTS_INBOX, r["filename"] or "")):
        if cand and os.path.isfile(cand):
            src = cand
            break
    if src:
        os.makedirs(RECEIPTS_TRASH, exist_ok=True)
        dest = os.path.join(RECEIPTS_TRASH, os.path.basename(src))
        n = 2
        while os.path.exists(dest):
            stem, ext = os.path.splitext(os.path.basename(src))
            dest = os.path.join(RECEIPTS_TRASH, f"{stem} ({n}){ext}")
            n += 1
        try:
            _shutil.move(src, dest)
        except OSError as e:
            return jsonify({"ok": False, "error": f"Couldn't move file: {e}"}), 500
    db.execute("""UPDATE receipts SET status='not_receipt', matched_trx_id=NULL,
                     notes=COALESCE(notes,'') || ' [not a receipt]',
                     updated_at=datetime('now') WHERE id=?""", (receipt_id,))
    db.commit()
    return jsonify({"ok": True})


def receipts_trash_view():
    """Receipts trash: view every trashed file with a BIG preview before
    deciding — delete one at a time, restore to inbox, or delete all. Files
    land here from 'Discard duplicate' and 'Not a receipt'."""
    files = []
    if os.path.isdir(RECEIPTS_TRASH):
        for name in sorted(os.listdir(RECEIPTS_TRASH)):
            p = os.path.join(RECEIPTS_TRASH, name)
            if name.startswith(".") or not os.path.isfile(p):
                continue
            files.append({
                "name": name,
                "path": to_mac_path(p),
                "ext": name.rsplit(".", 1)[-1].lower() if "." in name else "",
                "size_kb": round(os.path.getsize(p) / 1024),
            })
    return render_template("receipts/trash_files.html", files=files)


def api_receipts_trash_file():
    """Act on ONE trashed file. Body: {name, action: 'delete'|'restore'}.
    'restore' moves it back to the inbox (rescannable); 'delete' is
    permanent. Name is basename-only — no path tricks."""
    import shutil as _shutil
    data = request.get_json(silent=True) or {}
    name = os.path.basename((data.get("name") or "").strip())
    action = data.get("action")
    if not name or action not in ("delete", "restore"):
        return jsonify({"ok": False, "error": "need name + action delete|restore"}), 400
    p = os.path.join(RECEIPTS_TRASH, name)
    if not os.path.isfile(p):
        return jsonify({"ok": False, "error": "file not found in trash"}), 404
    try:
        if action == "delete":
            os.remove(p)
        else:
            os.makedirs(RECEIPTS_INBOX, exist_ok=True)
            dest = os.path.join(RECEIPTS_INBOX, name)
            n = 2
            while os.path.exists(dest):
                stem, ext = os.path.splitext(name)
                dest = os.path.join(RECEIPTS_INBOX, f"{stem} ({n}){ext}")
                n += 1
            _shutil.move(p, dest)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "action": action})


def receipts_empty_trash():
    """Permanently delete every file in receipts/trash/ (the discarded
    duplicates + not-a-receipt files). User-initiated only, behind a
    confirm — this is the disk-space cleanup button."""
    deleted, failed = 0, 0
    if os.path.isdir(RECEIPTS_TRASH):
        for name in os.listdir(RECEIPTS_TRASH):
            p = os.path.join(RECEIPTS_TRASH, name)
            if os.path.isfile(p) and not name.startswith("."):
                try:
                    os.remove(p)
                    deleted += 1
                except OSError:
                    failed += 1
    return jsonify({"ok": True, "deleted": deleted, "failed": failed})


def receipts_trx_search():
    """Vendor-substring search for transactions. Used by the orphans page to
    pick the right trx to match a receipt to. Returns each candidate's id,
    date, vendor, amount, category, AND a `has_receipt` flag so the user can
    see which trxs are already filed against (and avoid double-attaching by
    accident).

    NOTE: distinct from /api/transactions/search (the link-picker), which
    has a different response shape.

    Query params (vendor, amount, or BOTH):
      q       — vendor / raw_description substring (optional if amount given)
      amount  — optional float; when given, FILTERS to ±10% of the value
                (min ±$0.50 so small amounts aren't impossibly tight)
      limit   — max results (default 20, max 50)
    """
    db    = get_db()
    q     = (request.args.get("q") or "").strip()
    amt   = request.args.get("amount", type=float)
    limit = min(request.args.get("limit", 20, type=int), 50)

    if len(q) < 2 and amt is None:
        return jsonify({"ok": True, "rows": []})

    filters = ["t.status='active'", "t.owner=?"]
    params  = [OWNER]
    if len(q) >= 2:
        like = f"%{q}%"
        filters.append("(t.vendor LIKE ? OR t.raw_description LIKE ? OR t.note LIKE ?)")
        params += [like, like, like]
    order = "t.trx_date DESC"
    select_extra = ""
    if amt is not None:
        tol = max(0.50, round(abs(amt) * 0.10, 2))
        filters.append("ABS(ABS(t.amount) - ?) <= ?")
        params += [abs(amt), tol]
        select_extra = ", ABS(ABS(t.amount) - ?) AS amt_diff"
        order = "amt_diff ASC, t.trx_date DESC"
    sql = f"""
        SELECT t.id, t.trx_date, t.vendor, t.raw_description, t.amount,
               t.owner, t.l1_category, t.l2_category,
               t.receipt_path IS NOT NULL AS has_receipt{select_extra}
          FROM transactions t
         WHERE {' AND '.join(filters)}
         ORDER BY {order}
         LIMIT ?
    """
    if amt is not None:
        params.insert(0, abs(amt))   # the SELECT's amt_diff placeholder
    rows = db.execute(sql, params + [limit]).fetchall()

    return jsonify({
        "ok": True,
        "rows": [dict(r) for r in rows],
    })


def _split_family_ids(db, trx_id):
    """Return all trx ids in the same split family as `trx_id` (parent +
    all children with the same parent_id). If the trx isn't part of a
    split, returns just [trx_id].

    Used to propagate receipt_path across siblings — every member of
    a split shares one receipt.
    """
    row = db.execute("SELECT parent_id FROM transactions WHERE id=?", (trx_id,)).fetchone()
    if row and row["parent_id"]:
        # trx is a child — family = its parent + all of that parent's children
        family = [row["parent_id"]]
        kids = db.execute(
            "SELECT id FROM transactions WHERE parent_id=?", (row["parent_id"],)
        ).fetchall()
        family.extend(k["id"] for k in kids)
        return family
    # trx may be a parent — pick up its children
    kids = db.execute(
        "SELECT id FROM transactions WHERE parent_id=?", (trx_id,)
    ).fetchall()
    if kids:
        return [trx_id] + [k["id"] for k in kids]
    return [trx_id]


# ─── Scan Inbox ──────────────────────────────────────────────────────────────

def api_scan_inbox():
    """Run the receipts pipeline (intake + match, review-by-default) right
    from the portal. Flask runs locally, so this just invokes
    process_receipts.py as a subprocess and reports what changed. Nothing
    auto-files — everything lands in the review queue / orphans."""
    import sys as _sys, subprocess as _sub
    db = get_db()

    def _counts():
        return {r["status"]: r["n"] for r in db.execute(
            "SELECT status, COUNT(*) n FROM receipts GROUP BY status")}

    before = _counts()
    script = os.path.join(BASE_DIR, "process_receipts.py")
    try:
        proc = _sub.run([_sys.executable, script, "--no-autofile"],
                        cwd=BASE_DIR, capture_output=True, text=True,
                        timeout=600)
    except _sub.TimeoutExpired:
        return jsonify({"ok": False, "error": "Scan timed out (10 min) — run from Terminal to inspect."}), 500
    # Reopen numbers on OUR connection (subprocess wrote via its own)
    after = _counts()
    delta = {k: after.get(k, 0) - before.get(k, 0)
             for k in set(before) | set(after)}
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-15:])
    return jsonify({
        "ok": proc.returncode == 0,
        "queued":  max(delta.get("queued", 0), 0),
        "orphans": max(delta.get("orphan", 0), 0),
        "dupes":   max(delta.get("suspected_duplicate", 0), 0),
        "filed":   max(delta.get("filed", 0), 0),
        "exit":    proc.returncode,
        "output_tail": tail,
    })


def tools_receipts():
    """Tools — Receipts landing. Cards show live pending counts:
    queued / orphans / suspected dupes."""
    db = get_db()
    counts = {r["status"]: r["n"] for r in db.execute(f"""
        SELECT r.status, COUNT(*) n FROM receipts r
         WHERE r.status IN ('queued','orphan','suspected_duplicate')
           AND {_receipts_owner_filter()}
         GROUP BY r.status""")}
    try:
        n_trash = len([f for f in os.listdir(RECEIPTS_TRASH)
                       if not f.startswith(".")]) if os.path.isdir(RECEIPTS_TRASH) else 0
    except OSError:
        n_trash = 0
    return render_template("tools/receipts_home.html",
        n_queued=counts.get("queued", 0),
        n_orphans=counts.get("orphan", 0),
        n_dupes=counts.get("suspected_duplicate", 0),
        n_total=sum(counts.values()),
        n_trash=n_trash)


def register(app, helpers):
    """Bind this module's routes. Endpoint names == function names,
    exactly as in the pre-split app.py."""
    login_required = helpers["login_required"]
    global receipt_preview, trx_receipt_add, receipt_delete, receipts_queue, \
        receipts_orphans, receipts_review, receipts_queue_reject, \
        receipts_confirm_file, receipts_discard, receipts_not_receipt, \
        receipts_trash_view, api_receipts_trash_file, receipts_empty_trash, \
        receipts_trx_search, api_scan_inbox, tools_receipts, amt
    amt = helpers["amt"]
    receipt_preview = login_required(receipt_preview)
    app.route("/api/receipts/preview")(receipt_preview)
    trx_receipt_add = login_required(trx_receipt_add)
    app.route("/api/transactions/<int:trx_id>/receipts", methods=["POST"])(trx_receipt_add)
    receipt_delete = login_required(receipt_delete)
    app.route("/api/receipts/<int:receipt_id>", methods=["DELETE"])(receipt_delete)
    receipts_queue = login_required(receipts_queue)
    app.route("/receipts/queue")(receipts_queue)
    receipts_orphans = login_required(receipts_orphans)
    app.route("/receipts/orphans")(receipts_orphans)
    receipts_review = login_required(receipts_review)
    app.route("/receipts/review")(receipts_review)
    receipts_queue_reject = login_required(receipts_queue_reject)
    app.route("/api/receipts/<int:receipt_id>/reject", methods=["POST"])(receipts_queue_reject)
    receipts_confirm_file = login_required(receipts_confirm_file)
    app.route("/api/receipts/<int:receipt_id>/confirm-file", methods=["POST"])(receipts_confirm_file)
    receipts_discard = login_required(receipts_discard)
    app.route("/api/receipts/<int:receipt_id>/discard", methods=["POST"])(receipts_discard)
    receipts_not_receipt = login_required(receipts_not_receipt)
    app.route("/api/receipts/<int:receipt_id>/not-receipt", methods=["POST"])(receipts_not_receipt)
    receipts_trash_view = login_required(receipts_trash_view)
    app.route("/receipts/trash")(receipts_trash_view)
    api_receipts_trash_file = login_required(api_receipts_trash_file)
    app.route("/api/receipts/trash-file", methods=["POST"])(api_receipts_trash_file)
    receipts_empty_trash = login_required(receipts_empty_trash)
    app.route("/api/receipts/empty-trash", methods=["POST"])(receipts_empty_trash)
    receipts_trx_search = login_required(receipts_trx_search)
    app.route("/api/receipts/trx-search", methods=["GET"])(receipts_trx_search)
    api_scan_inbox = login_required(api_scan_inbox)
    app.route("/api/receipts/scan-inbox", methods=["POST"])(api_scan_inbox)
    tools_receipts = login_required(tools_receipts)
    app.route("/tools/receipts")(tools_receipts)
