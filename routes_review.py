"""
routes_review.py — review queue + import (CSV/manual), trash, undo batch, review rules, admin seed.

Personal Financial Tracker (PFT): single owner (config.OWNER).
No blueprints: register(app, helpers) binds every view under its original
function name, so endpoint names, url_for(...) and base.html `ep ==` checks
are unchanged.
"""
import os
from flask import (request, redirect, url_for,
                   render_template, jsonify, flash)
from dedup_utils import find_duplicate
from vendor_rules import is_airline, skip_reason
from config import (
    OWNER, IMPORTS_CHECKING_DIR, IMPORTS_CC_DIR, IMPORTS_OTHER_DIR,
)
from db import get_db
from billing import calc_payment_dates


def _imports_dir_for_account_type(acct_type: str) -> str:
    """Return the right imports/<sub>/ folder for an account type.
    Creates the dir if missing. Used by the CSV importer to file uploads
    automatically (no flat 'inbox' dump)."""
    if acct_type == "credit_card":
        d = IMPORTS_CC_DIR
    elif acct_type == "checking":
        d = IMPORTS_CHECKING_DIR
    else:
        d = IMPORTS_OTHER_DIR
    os.makedirs(d, exist_ok=True)
    return d


def _save_imported_csv(file_storage, acct_type: str) -> str:
    """Save an uploaded CSV (FileStorage) to the right imports/<sub>/
    folder with a UTC timestamp prefix so re-uploads never clobber
    history. Returns the absolute path on disk."""
    from datetime import datetime as _dt
    from werkzeug.utils import secure_filename as _secure_filename
    safe_name = _secure_filename(file_storage.filename) or "import.csv"
    target_dir = _imports_dir_for_account_type(acct_type)
    ts = _dt.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(target_dir, f"{ts}_{safe_name}")
    # Rewind first: depending on werkzeug version / prior form access, the
    # upload stream can be left at EOF, which makes save() write an EMPTY file
    # → the importer then reports "file appears empty". Seeking to 0 is a no-op
    # on a fresh stream and fixes the consumed-stream case.
    try:
        file_storage.stream.seek(0)
    except Exception:
        pass
    file_storage.save(path)
    # Belt-and-suspenders: if the save still landed empty but the stream has
    # bytes, write them directly.
    try:
        if os.path.getsize(path) == 0:
            file_storage.stream.seek(0)
            data = file_storage.stream.read()
            if data:
                with open(path, "wb") as _fh:
                    _fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    except Exception:
        pass
    return path

# ─── Review-queue auto-categorization rules ─────────────────────────────────
# Vendor-pattern overrides applied to every row of a CSV import after the
# importer's generic classify() but before the staging insert. Matches are
# case-insensitive against vendor + raw_description. First match wins.
#
# Each rule sets l1_category/l2_category. Categories must exist in the
# starter tree (categories.py) — or whatever tree your database holds.
# This is a small starter set; add your own recurring vendors here (or ask
# your AI assistant — see docs/customize_with_ai.md).

def _apply_review_rules(r):
    """Mutates r in place. Returns nothing."""
    vendor   = (r.get("vendor") or "").lower()
    raw_desc = (r.get("raw_description") or "").lower()
    text     = vendor + " " + raw_desc

    # Airlines (Southwest, Delta, United, American, …) → Travel/Airfare
    if is_airline(r.get("vendor")):
        r["l1_category"] = "Travel"
        r["l2_category"] = "Airfare"
        return

    # Costco — but NOT Costco Gas (which is gas, not groceries)
    if "costco" in vendor and "gas" not in text:
        r["l1_category"] = "Food & Dining"
        r["l2_category"] = "Groceries"
        return

    # Uber / Lyft → rideshare (but NOT Uber Eats, which is dining)
    if "uber eats" in text or "ubereats" in text:
        r["l1_category"] = "Food & Dining"
        r["l2_category"] = "Restaurants"
        return
    if "lyft" in text or "uber" in text:
        r["l1_category"] = "Transportation"
        r["l2_category"] = "Rideshare & Taxi"
        return

    # Netflix → streaming
    if "netflix" in text:
        r["l1_category"] = "Entertainment"
        r["l2_category"] = "Streaming & Subscriptions"
        return

    # Gyms → fitness (applies to credits too — a credit comes through as
    # trx_type='expense' with negative amount; the category still belongs
    # to fitness)
    if "planet fitness" in text or "24 hour fitness" in text:
        r["l1_category"] = "Health & Fitness"
        r["l2_category"] = "Gym & Fitness"
        return

    # Target → groceries
    if "target" in vendor:
        r["l1_category"] = "Food & Dining"
        r["l2_category"] = "Groceries"
        return


# ─── Import ───────────────────────────────────────────────────────────────────

def _skip_summary(skipped):
    """Human-readable, specific reason for skipped rows (so an import can never
    fail silently). `skipped` is a list of {reason, sample, ...}."""
    header  = next((s for s in skipped if s["reason"] == "header"), None)
    amount  = [s for s in skipped if s["reason"] == "amount"]
    missing = [s for s in skipped if s["reason"] == "missing_date_or_desc"]
    parts = []
    if header:
        parts.append(
            f"this doesn't look like a {header.get('kind','matching')} export "
            f"(missing the \"{header.get('expected','expected')}\" column) — "
            f"make sure you selected the right account. File header: "
            f"{header.get('sample')}")
    if amount:
        eg = next((s.get("sample") for s in amount if s.get("sample")), "")
        parts.append(f"{len(amount)} row(s) with an unparseable Amount"
                     + (f" (e.g. \"{eg}\")" if eg else ""))
    # A header mismatch also trips the missing-date guard on every row — that's
    # a consequence, not a separate problem, so only report it on its own.
    if missing and not header:
        parts.append(f"{len(missing)} row(s) missing a date/description")
    return "; ".join(parts) + "." if parts else f"{len(skipped)} row(s) skipped."


def import_csv():
    """CSV import — upload → parse → auto-cat rules → staging → user
    reviews and approves from the review queue."""
    db = get_db()
    # Only CSV-importable accounts in the upload dropdown.
    # Checking first (not alphabetical) — an alphabetically-first credit card
    # used to win the old default and imported checking files against the CC
    # parser, the mute-failure trigger.
    accounts = db.execute(
        "SELECT * FROM accounts WHERE owner=? AND active=1 AND type IN ('credit_card','checking','digital_wallet') "
        "ORDER BY (type='checking') DESC, name",
        (OWNER,)
    ).fetchall()

    if request.method == "POST":
        account_id = request.form.get("account_id", type=int)
        f = request.files.get("csv_file")

        if not f or not account_id:
            flash("Please select an account and a CSV file.", "error")
            return redirect(url_for("import_csv"))

        # Determine account type so we know which imports/<sub>/ folder
        # to save into (checking vs creditcards).
        acct = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not acct:
            flash("Unknown account.", "error")
            return redirect(url_for("import_csv"))

        # Auto-file the raw CSV: imports/<checking|creditcards>/<UTC>_<name>.csv
        filepath = _save_imported_csv(f, acct["type"])

        if acct["type"] == "credit_card":
            from importers.chase_cc import parse
        elif acct["type"] == "digital_wallet":
            from importers.venmo import parse
        else:
            from importers.chase_checking import parse

        rows, dupes, skipped = parse(filepath, account_id, db)

        if not rows:
            if skipped:
                # Never fail mute: say WHY every row was dropped (the classic
                # case is currency-formatted Amounts like "$1,234.56" from a
                # hand-edited Excel export, or the wrong CSV for this account).
                flash("Parsed 0 importable rows — " + _skip_summary(skipped)
                      + " Check the file's number formatting / that it matches "
                      "this account's export type.", "error")
            else:
                flash("Nothing to import — file appears empty.", "info")
            return redirect(url_for("import_csv"))

        # Apply vendor auto-categorization rules to each row.
        for r in rows:
            _apply_review_rules(r)

        new_count = len([r for r in rows if not r.get("is_dupe") and not r.get("is_skip")])
        auto_skipped = len([r for r in rows if r.get("is_skip") and not r.get("is_dupe")])

        # Create import batch
        batch = db.execute("""
            INSERT INTO import_batches (filename, account_id, row_count, dupes_count)
            VALUES (?, ?, ?, ?)
        """, (f.filename, account_id, len(rows), dupes))
        batch_id = batch.lastrowid

        # Insert ALL rows into staging — dupes get 'duplicate', card-bill
        # payments get 'skipped' (both segregated from the pending queue).
        for r in rows:
            status = ("duplicate" if r.get("is_dupe")
                      else "skipped" if r.get("is_skip")
                      else "pending")
            db.execute("""
                INSERT INTO staging
                  (import_batch_id, account_id,
                   raw_trx_date, raw_post_date, raw_description, raw_amount, raw_category, raw_type,
                   trx_date, post_date, statement_date, vendor, amount, trx_type,
                   l1_category, l2_category, note,
                   owner, dedup_key, status, dup_of_trx_id, flag_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                batch_id, account_id,
                r["raw_trx_date"], r.get("raw_post_date"), r["raw_description"],
                r["raw_amount"], r.get("raw_category"), r.get("raw_type"),
                r["trx_date"], r.get("post_date"), r.get("statement_date"),
                r.get("vendor"),
                r["amount"], r.get("trx_type", "expense"),
                r.get("l1_category"), r.get("l2_category"), r.get("note"),
                OWNER,
                r["dedup_key"], status, r.get("dup_of_trx_id"), r.get("flag_reason"),
            ))
        db.commit()

        msg = f"Imported {new_count} new transaction(s)"
        if auto_skipped:
            msg += f" · {auto_skipped} auto-skipped (card-side payment rows — the checking-side payment imports as a CC Payment transfer)"
        if dupes:
            msg += f" · {dupes} duplicate(s) flagged for review"
        if skipped:
            msg += f" · {_skip_summary(skipped)}"
        flash(msg + ". Review below.", "success")
        return redirect(url_for("review_queue"))

    # Recent batches — with snapshot stats from staging
    batches = db.execute("""
        SELECT b.*, a.name as account_name,
               COUNT(s.id)        as staged_count,
               COALESCE(SUM(s.amount), 0) as staged_total,
               MIN(s.trx_date)    as date_start,
               MAX(s.trx_date)    as date_end
        FROM import_batches b
        JOIN accounts a ON b.account_id = a.id
        LEFT JOIN staging s ON s.import_batch_id = b.id
        GROUP BY b.id
        ORDER BY b.imported_at DESC LIMIT 10
    """).fetchall()

    # NO default selection: the dropdown always opens on the blank
    # "— Select account —" so an explicit choice is forced every time — any
    # default invites autopilot mis-imports. `autocomplete="off"` in the
    # template stops the browser restoring a prior session's pick. The
    # checking-first ORDER BY + type labels stay (harmless, aids the choice).
    return render_template("import.html",
        accounts=accounts,
        batches=batches,
    )

# ─── Review Queue ─────────────────────────────────────────────────────────────

def review_queue():
    db = get_db()
    # `show_discarded=1` switches the queue to a discarded-only view (no
    # mixing with pending). Discarded rows display Restore / Delete actions.
    show_discarded = request.args.get("show_discarded", "0") == "1"
    statuses = ('discarded',) if show_discarded else ('pending','duplicate','skipped')
    placeholders = ",".join("?"*len(statuses))
    raw_rows = db.execute(f"""
        SELECT s.*, a.name as account_name, a.account_num as account_num,
               dt.vendor AS dup_vendor, dt.trx_date AS dup_date,
               dt.amount AS dup_amount, dt.owner AS dup_owner
        FROM staging s JOIN accounts a ON s.account_id = a.id
        LEFT JOIN transactions dt ON dt.id = s.dup_of_trx_id
        WHERE s.status IN ({placeholders})
          AND s.owner = ?
        ORDER BY CASE s.status WHEN 'pending' THEN 0 WHEN 'skipped' THEN 1
                               WHEN 'duplicate' THEN 2 ELSE 3 END,
                 s.trx_date ASC, s.id ASC
    """, statuses + (OWNER,)).fetchall()

    # Pre-compute auto statement_date for rows that don't have one yet
    rows = []
    for r in raw_rows:
        r = dict(r)
        if not r.get("statement_date") and r.get("post_date") and r.get("account_num"):
            stmt, pay = calc_payment_dates(db, r["post_date"], r["account_num"])
            r["statement_date"] = stmt
            r["payment_date"] = pay
        rows.append(r)

    categories = db.execute(
        "SELECT * FROM categories ORDER BY trx_type, l1, l2"
    ).fetchall()

    # Build L1→L2 map for JS (keyed as "type:L1")
    cat_map = {}
    for c in categories:
        key = f"{c['trx_type']}:{c['l1']}"
        cat_map.setdefault(key, []).append(c["l2"])

    accounts = db.execute("SELECT * FROM accounts WHERE active=1 ORDER BY owner, name").fetchall()

    # Default L2 for the 'CC Payment' display choice: when exactly one
    # active credit-card account exists, its name pre-fills (same default
    # the checking importer applies); otherwise the user picks the card.
    _cc_names = [a["name"] for a in accounts if a["type"] == "credit_card"]
    default_cc_l2 = _cc_names[0] if len(_cc_names) == 1 else ""

    return render_template("review_queue.html",
        rows=rows,
        cat_map=cat_map,
        accounts=accounts,
        categories=categories,
        show_discarded=show_discarded,
        default_cc_l2=default_cc_l2,
    )

# ─── Review Queue API ─────────────────────────────────────────────────────────

def _auto_no_receipt(db, account_id, raw_description, vendor,
                     amount=None, trx_type=None):
    """Rows that never need receipts get flagged at insert so they never
    show up in Missing Receipts:
      - anything on a digital wallet (e.g. Venmo — manual-only; no receipts
        exist)
      - contra-expenses (credits/refunds: expense stored negative) —
        usually they later LINK to the original purchase and adopt its
        receipt, at which point the flag auto-clears (receipt sync)
    Returns 0/1 for the no_receipt_needed column."""
    if trx_type == "expense" and amount is not None and amount < 0:
        return 1
    acct = db.execute("SELECT type FROM accounts WHERE id=?",
                      (account_id,)).fetchone()
    if acct and acct["type"] == "digital_wallet":
        return 1
    return 0


def review_approve(staging_id):
    db   = get_db()
    data = request.get_json(silent=True) or {}
    row  = db.execute("SELECT * FROM staging WHERE id=?", (staging_id,)).fetchone()

    if not row:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if row["status"] != "pending":
        return jsonify({"ok": False, "error": "Already reviewed"}), 400

    # Fields from the payload (user may have edited before approving).
    # (2026-07-03 fix: was bare `or`-fallbacks, which silently reverted
    # user-cleared fields — and a 0 amount — to the staged values. Now a key
    # present-but-empty means "cleared" for nullable fields; required fields
    # still fall back.)
    def _field(key, fallback, allow_clear=True):
        if key in data:
            v = data[key]
            if v in ("", None):
                return None if allow_clear else fallback
            return v
        return fallback

    trx_date   = _field("trx_date", row["trx_date"], allow_clear=False)
    vendor     = _field("vendor", row["vendor"])
    trx_type   = _field("trx_type", row["trx_type"], allow_clear=False) or "expense"
    l1         = _field("l1_category", row["l1_category"])
    l2         = _field("l2_category", row["l2_category"])
    note       = _field("note", row["note"])
    # Garbage in the numeric fields must be a clean 400, not a traceback
    # (and never a TEXT value stored in a numeric column).
    try:
        amount = float(_field("amount", row["amount"], allow_clear=False))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "amount must be a number"}), 400
    try:
        account_id = int(_field("account_id", row["account_id"],
                                allow_clear=False))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "account_id must be a number"}), 400

    # Derive statement_date if not provided
    statement_date = _field("statement_date", row["statement_date"])
    post_date      = _field("post_date", row["post_date"])

    # Look up the account to get account_num for billing cycle calc
    acct = db.execute(
        "SELECT account_num FROM accounts WHERE id=?", (account_id,)
    ).fetchone()
    account_num = acct["account_num"] if acct else None

    # Auto-fill statement + payment dates for any known CC card. Each is
    # computed independently — historically this only fired when statement
    # was missing, which left payment_date NULL on rows where statement
    # had been filled by an earlier code path. Now: if EITHER is missing
    # and we can compute, we fill it.
    payment_date = _field("payment_date", row["payment_date"])
    if post_date and account_num and (not statement_date or not payment_date):
        stmt_auto, pay_auto = calc_payment_dates(db, post_date, account_num)
        if not statement_date and stmt_auto:
            statement_date = stmt_auto
        if not payment_date and pay_auto:
            payment_date = pay_auto

    # Sign convention flip on approve:
    # Staging stores raw bank sign (+ = money in, − = money out). Transactions
    # uses semantic sign per type:
    #   expense: + = expense outflow, − = contra-expense / credit
    #   income / transfer: keep the staging sign as-is
    final_amount = float(amount)
    if trx_type == "expense":
        final_amount = -final_amount

    db.execute("""
        INSERT INTO transactions
          (account_id, staging_id, trx_date, post_date, statement_date, payment_date,
           raw_description, vendor, amount, trx_type, owner,
           l1_category, l2_category, note, dedup_key, no_receipt_needed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        account_id, staging_id, trx_date, post_date, statement_date, payment_date,
        row["raw_description"], vendor, final_amount, trx_type, OWNER,
        l1, l2, note, row["dedup_key"],
        _auto_no_receipt(db, account_id, row["raw_description"], vendor,
                         amount=final_amount, trx_type=trx_type),
    ))

    db.execute(
        "UPDATE staging SET status='approved', reviewed_at=datetime('now') WHERE id=?",
        (staging_id,)
    )

    db.commit()

    # Auto-sync: if this approval was an investment transfer, land it in the
    # lot engine immediately — no manual Sync needed. Idempotent + guarded.
    if trx_type == "transfer" and l1 in ("Retirement", "General Savings", "Alternatives"):
        import routes_investments as _inv
        _inv.auto_sync_after_change(db)
        db.commit()

    return jsonify({"ok": True})

def review_restore(staging_id):
    """Bring a staging row back into the review queue.

    Two cases, by source status:

    * From 'discarded'/'trashed' → RE-EVALUATE the flags exactly as on import
      (dupe wins, then skip, else clean pending). So restoring a discarded
      duplicate/CC-payment/wallet-row lands back FLAGGED, not as a valid charge.
    * From 'skipped'/'duplicate' (the ✓ button on a flagged in-queue row) →
      explicit rescue: force 'pending' and clear the flag, since the user is
      saying "this one is actually valid."

    Legacy auto-ignore rows (trx_type='ignore') get a sane type either way.
    """
    db  = get_db()
    row = db.execute("SELECT * FROM staging WHERE id=?", (staging_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Not found"}), 404

    cur = row["status"]
    # Re-type legacy 'ignore' rows by amount sign so a rescued row is usable.
    amt = float(row["amount"] or 0)
    if row["trx_type"] == "ignore":
        new_type = "income" if amt > 0 else ("expense" if amt < 0 else "transfer")
    else:
        new_type = row["trx_type"]

    if cur in ("discarded", "trashed"):
        # Re-evaluate flags as if freshly imported (dupe > skip > pending).
        dup = find_duplicate(db, row["account_id"], row["trx_date"],
                             row["raw_description"], row["amount"],
                             exclude_staging_id=staging_id)
        if dup:
            new_status   = "duplicate"
            flag_reason  = "Duplicate"
            dup_of       = dup["id"] if dup["kind"] == "transaction" else None
        else:
            sk = skip_reason(row["raw_description"])
            if sk:
                new_status, flag_reason, dup_of = "skipped", sk, None
            else:
                new_status, flag_reason, dup_of = "pending", None, None
        db.execute("""
            UPDATE staging
               SET status=?, flag_reason=?, dup_of_trx_id=?,
                   trx_type=?, reviewed_at=NULL
             WHERE id=?
        """, (new_status, flag_reason, dup_of, new_type, staging_id))
    elif cur in ("skipped", "duplicate"):
        # Explicit rescue from the queue → valid, unflagged, pending.
        db.execute("""
            UPDATE staging
               SET status='pending', flag_reason=NULL, dup_of_trx_id=NULL,
                   trx_type=?, reviewed_at=NULL
             WHERE id=?
        """, (new_type, staging_id))
    else:
        return jsonify({"ok": False, "error": "Not restorable"}), 400

    db.commit()
    return jsonify({"ok": True})


def review_hard_delete(staging_id):
    """Move a discarded staging row to the Trash (recoverable). Only works on
    rows already in 'discarded' status. The dedup key is kept until the row is
    permanently purged from the Trash view."""
    db  = get_db()
    row = db.execute(
        "SELECT id FROM staging WHERE id=? AND status='discarded'",
        (staging_id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Not found or not discarded"}), 404
    db.execute("UPDATE staging SET status='trashed' WHERE id=?", (staging_id,))
    db.commit()
    return jsonify({"ok": True})


def review_delete_all_discarded():
    """Move every discarded staging row to the Trash."""
    db = get_db()
    n = db.execute(
        "UPDATE staging SET status='trashed' WHERE status='discarded' AND owner=?",
        (OWNER,)
    ).rowcount
    db.commit()
    return jsonify({"ok": True, "deleted": n})


# ─── Trash (recycle bin) ──────────────────────────────────────────────────────
def trash_view():
    """Unified recycle bin: user-deleted transactions + trashed staging rows.
    Both recoverable (Restore) or removable for good (Purge). Split parents
    (is_split=1, status='deleted') are excluded — they're split artifacts."""
    db = get_db()
    trx = db.execute("""
        SELECT t.id, t.trx_date, t.vendor, t.raw_description, t.amount,
               t.owner, t.l1_category, t.l2_category, a.name AS account_name
          FROM transactions t JOIN accounts a ON t.account_id=a.id
         WHERE t.status='deleted' AND COALESCE(t.is_split,0)=0
         ORDER BY t.trx_date DESC, t.id DESC
    """).fetchall()
    stg = db.execute("""
        SELECT s.id, s.trx_date, s.vendor, s.raw_description, s.amount,
               s.owner, s.flag_reason, a.name AS account_name
          FROM staging s JOIN accounts a ON s.account_id=a.id
         WHERE s.status='trashed'
         ORDER BY s.trx_date DESC, s.id DESC
    """).fetchall()
    return render_template("trash.html", trx=trx, stg=stg)


def trx_restore(trx_id):
    """Restore a trashed (soft-deleted) transaction back to active."""
    db = get_db()
    db.execute("UPDATE transactions SET status='active' WHERE id=? AND status='deleted'",
               (trx_id,))
    db.commit()
    return jsonify({"ok": True})


def _free_key_if_unused(db, dedup_key):
    if not dedup_key:
        return
    still = (db.execute("SELECT 1 FROM transactions WHERE dedup_key=? LIMIT 1",
                        (dedup_key,)).fetchone()
             or db.execute("SELECT 1 FROM staging WHERE dedup_key=? LIMIT 1",
                           (dedup_key,)).fetchone())
    if not still:
        db.execute("DELETE FROM known_dedup_keys WHERE dedup_key=?", (dedup_key,))


def trx_purge(trx_id):
    """Permanently delete a trashed transaction (+ free its dedup key)."""
    db = get_db()
    row = db.execute("SELECT dedup_key FROM transactions WHERE id=? AND status='deleted'",
                     (trx_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Not found or not in trash"}), 404
    db.execute("DELETE FROM transaction_links WHERE a_id=? OR b_id=?", (trx_id, trx_id))
    db.execute("UPDATE receipts SET matched_trx_id=NULL WHERE matched_trx_id=?", (trx_id,))
    # CC-recon: the row may still be a settled charge / reconciled payment
    # (cc_settlements FK) — auto-unwind, or the DELETE hits the FK.
    try:
        import routes_ccrecon as _ccr
        _ccr.auto_unwind_for_trx(db, trx_id)
    except Exception:
        pass
    db.execute("DELETE FROM transactions WHERE id=?", (trx_id,))
    _free_key_if_unused(db, row["dedup_key"])
    db.commit()
    return jsonify({"ok": True})


def staging_purge(staging_id):
    """Permanently delete a trashed staging row (+ free its dedup key)."""
    db = get_db()
    row = db.execute("SELECT dedup_key FROM staging WHERE id=? AND status='trashed'",
                     (staging_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Not found or not in trash"}), 404
    db.execute("DELETE FROM staging WHERE id=?", (staging_id,))
    _free_key_if_unused(db, row["dedup_key"])
    db.commit()
    return jsonify({"ok": True})


def undo_import_batch(batch_id):
    """Undo a single import batch — completely.

    Removes:
      • any approved transactions sourced from this batch (via staging_id)
      • all staging rows for this batch
      • known_dedup_keys whose (key, account_id) is no longer referenced
        by any other staging or transactions row
      • the import_batches row itself

    Replaces the prior ad-hoc 'wipe by account_num' SQL one-liner pattern,
    which was a sledgehammer (nuked every batch on the account) and missed
    known_dedup_keys cleanup, leading to the duplicate-flag bug.
    """
    db = get_db()
    batch = db.execute(
        """SELECT b.*, a.name AS account_name, a.account_num
             FROM import_batches b JOIN accounts a ON b.account_id=a.id
            WHERE b.id=?""",
        (batch_id,)
    ).fetchone()

    if not batch:
        return jsonify({"ok": False, "error": "Batch not found"}), 404

    # Capture (dedup_key, account_id) pairs from this batch's staging rows
    # before we delete them — needed to reason about kdk cleanup afterward.
    pairs = db.execute(
        "SELECT DISTINCT dedup_key, account_id FROM staging WHERE import_batch_id=?",
        (batch_id,)
    ).fetchall()

    # 1) Approved transactions sourced from this batch (via staging_id FK).
    #    (2026-07-03 fix: was a bare DELETE, which 500'd on FK violations —
    #    receipts.matched_trx_id has no cascade, and split children's
    #    parent_id blocks deleting their parent.)
    batch_trx_ids = [r["id"] for r in db.execute(
        """SELECT id FROM transactions
            WHERE staging_id IN (SELECT id FROM staging WHERE import_batch_id=?)""",
        (batch_id,))]
    all_ids = list(batch_trx_ids)
    if batch_trx_ids:
        ph = ",".join("?" * len(batch_trx_ids))
        all_ids += [r["id"] for r in db.execute(
            f"SELECT id FROM transactions WHERE parent_id IN ({ph})",
            batch_trx_ids)]
    trx_deleted = 0
    if all_ids:
        ph = ",".join("?" * len(all_ids))
        # Detach receipts (files stay on disk; rows return to the orphans
        # queue so they can be re-matched after a re-import).
        db.execute(f"""
            UPDATE receipts SET matched_trx_id=NULL, status='orphan',
                   notes=COALESCE(notes,'') || ' [detached by import undo]',
                   updated_at=datetime('now')
             WHERE matched_trx_id IN ({ph})
        """, all_ids)
        # Children first (parent_id FK), then the batch rows themselves.
        # (transaction_links + transaction_tags cascade via their FKs.)
        trx_deleted += db.execute(
            f"DELETE FROM transactions WHERE parent_id IN ({ph})", all_ids
        ).rowcount
        trx_deleted += db.execute(
            f"DELETE FROM transactions WHERE id IN ({ph})", all_ids
        ).rowcount

    # 2) Staging rows for this batch.
    staging_deleted = db.execute(
        "DELETE FROM staging WHERE import_batch_id=?",
        (batch_id,)
    ).rowcount

    # 3) Free kdk only when no other staging/trx row still references it.
    #    (A duplicate import in another batch could legitimately keep the key.)
    kdk_freed = 0
    for p in pairs:
        still_used = db.execute(
            """SELECT 1 FROM staging
                WHERE dedup_key=? AND account_id=? LIMIT 1""",
            (p["dedup_key"], p["account_id"])
        ).fetchone() or db.execute(
            """SELECT 1 FROM transactions
                WHERE dedup_key=? AND account_id=? LIMIT 1""",
            (p["dedup_key"], p["account_id"])
        ).fetchone()
        if not still_used:
            db.execute(
                "DELETE FROM known_dedup_keys WHERE dedup_key=? AND account_id=?",
                (p["dedup_key"], p["account_id"])
            )
            kdk_freed += 1

    # 4) The batch shell itself.
    db.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))

    db.commit()

    return jsonify({
        "ok":              True,
        "filename":        batch["filename"],
        "account_name":    batch["account_name"],
        "trx_deleted":     trx_deleted,
        "staging_deleted": staging_deleted,
        "kdk_freed":       kdk_freed,
    })


def review_skip(staging_id):
    db = get_db()
    db.execute(
        "UPDATE staging SET status='skipped', reviewed_at=datetime('now') WHERE id=?",
        (staging_id,)
    )
    db.commit()
    return jsonify({"ok": True})

def review_discard(staging_id):
    db = get_db()
    db.execute(
        "UPDATE staging SET status='discarded', reviewed_at=datetime('now') WHERE id=?",
        (staging_id,)
    )
    db.commit()
    return jsonify({"ok": True})

def review_update(staging_id):
    """Save edits to a staging row without approving."""
    db   = get_db()
    data = request.get_json(silent=True) or {}
    allowed = {"trx_date", "post_date", "statement_date", "payment_date",
               "vendor", "amount", "trx_type",
               "l1_category", "l2_category", "note", "account_id"}
    # Keep numeric columns numeric — SQLite would store a TEXT amount as-is
    # and every later sum/abs() silently mis-counts or crashes.
    for k, cast in (("amount", float), ("account_id", int)):
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
            # (2026-07-03 fix: was `v or None`, which turned a legitimate 0
            # amount into SQL NULL. Only empty string means "clear".)
            vals.append(v if v not in ("", None) else None)
    if not sets:
        return jsonify({"ok": True})
    vals.append(staging_id)
    db.execute(f"UPDATE staging SET {', '.join(sets)} WHERE id=?", vals)
    db.commit()
    return jsonify({"ok": True})

# ─── Manual Entry ─────────────────────────────────────────────────────────────

def import_manual():
    """Manual entry — form → direct insert to transactions (no staging)."""
    db = get_db()
    # Writable accounts for manual entry: all active accounts that aren't
    # investment-tracked (those have their own machinery), with digital
    # wallets (e.g. Venmo) sorted first as the natural default.
    accounts = db.execute("""
        SELECT * FROM accounts
         WHERE active=1
           AND type != 'investment'
           AND (owner=? OR type='digital_wallet')
         ORDER BY (type='digital_wallet') DESC, name
    """, (OWNER,)).fetchall()
    categories = db.execute("SELECT * FROM categories ORDER BY trx_type, l1, l2").fetchall()

    cat_map = {}
    for c in categories:
        key = f"{c['trx_type']}:{c['l1']}"
        cat_map.setdefault(key, []).append(c["l2"])

    if request.method == "POST":
        import hashlib
        trx_date   = request.form.get("trx_date", "")
        vendor     = request.form.get("vendor", "").strip()
        trx_type   = request.form.get("trx_type", "expense")
        l1         = request.form.get("l1_category", "") or None
        l2         = request.form.get("l2_category", "") or None
        note       = request.form.get("note", "").strip() or None
        # A non-numeric amount / account id must flash, not traceback.
        try:
            amount = float(request.form.get("amount") or 0)
        except (TypeError, ValueError):
            flash("Amount must be a number.", "error")
            return redirect(url_for("import_manual"))
        try:
            account_id = int(request.form.get("account_id") or 0)
        except (TypeError, ValueError):
            account_id = 0
        if not account_id or not db.execute(
                "SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
            flash("Pick a valid account.", "error")
            return redirect(url_for("import_manual"))
        if not trx_date:
            flash("Date is required.", "error")
            return redirect(url_for("import_manual"))

        # 'CC Payment' is a DISPLAY fork over transfer (same stored type,
        # P&L-neutral) — the distinguishing signal is the L1 category.
        if trx_type == "cc_payment":
            trx_type = "transfer"
            l1 = l1 or "Credit Card Payment"

        owner = OWNER

        # Form label is "Vendor" — write the user value to BOTH `vendor` and
        # `raw_description` (the latter is NOT NULL in schema and is normally
        # the verbatim bank text on imports; on a manual entry there is no
        # bank text, so the vendor value is mirrored).
        raw_desc   = vendor or "Manual Entry"
        dedup_raw  = f"{trx_date}|{raw_desc.upper()}|{amount:.2f}"
        dedup_key  = hashlib.md5(dedup_raw.encode()).hexdigest()

        # Dates: the account dropdown includes credit cards — use the
        # DB-driven billing calc (accounts.stmt_close_day / pay_due_day)
        # when the account has a close day; non-CC accounts (and cards
        # without a close day yet) collapse stmt/payment to trx_date.
        post_date = trx_date
        acct_row = db.execute("SELECT account_num FROM accounts WHERE id=?",
                              (account_id,)).fetchone()
        acct_num = acct_row["account_num"] if acct_row else None
        statement_date, payment_date = calc_payment_dates(db, post_date, acct_num)
        statement_date = statement_date or trx_date
        payment_date   = payment_date or trx_date

        db.execute("""
            INSERT INTO transactions
              (account_id, trx_date, post_date, statement_date, payment_date,
               raw_description, vendor, amount,
               trx_type, owner, l1_category, l2_category, note, dedup_key,
               no_receipt_needed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (account_id, trx_date, post_date, statement_date, payment_date,
              raw_desc, vendor, amount,
              trx_type, owner, l1, l2, note, dedup_key,
              _auto_no_receipt(db, account_id, raw_desc, vendor,
                               amount=amount, trx_type=trx_type)))
        db.commit()
        flash(f"Transaction added: {vendor} — ${amount:,.2f}", "success")
        return redirect(url_for("import_manual"))

    from datetime import date
    return render_template("import_manual.html",
        accounts=accounts,
        cat_map=cat_map,
        today=date.today().isoformat(),
    )

# ─── Cash-basis date helpers ──────────────────────────────────────────────────
# calc_payment_dates lives in billing.py now (imported at the top): it is
# DB-driven from the accounts table (stmt_close_day / pay_due_day on each
# credit-card account) instead of the retired static CC_BILLING_CYCLES.


# ─── Admin / Dev helpers ──────────────────────────────────────────────────────

def admin_seed_review():
    """Insert fake staging rows so the review queue has data to test with.
    POST-only (2026-07-03 security pass): a GET that mutates the DB can be
    triggered by any <img src=...> on any webpage. Use:
    curl -X POST http://127.0.0.1:5005/admin/seed-review -b <session cookie>"""
    db = get_db()
    existing = db.execute("SELECT COUNT(*) FROM staging WHERE status='pending'").fetchone()[0]
    if existing >= 5:
        flash(f"Review queue already has {existing} pending rows — skipping seed.", "info")
        return redirect(url_for("review_queue"))

    accts = db.execute(
        "SELECT id FROM accounts WHERE owner=? AND active=1 ORDER BY id",
        (OWNER,)
    ).fetchall()

    # Spread the fake rows across the first two accounts (or one, or a
    # last-resort id=1 if the accounts table is somehow empty).
    first_id  = accts[0]["id"] if accts else 1
    second_id = accts[1]["id"] if len(accts) > 1 else first_id

    import hashlib
    fake_rows = [
        ("2026-01-15", "2026-01-17", "Amazon",           first_id,  45.99,  "expense"),
        ("2026-01-22", "2026-01-23", "Whole Foods",      first_id,  127.43, "expense"),
        ("2026-02-03", "2026-02-04", "DoorDash",         second_id, 38.75,  "expense"),
        ("2026-02-14", "2026-02-15", "Delta Air Lines",  second_id, 312.00, "expense"),
        ("2026-03-01", "2026-03-02", "Spotify",          first_id,  10.99,  "expense"),
        ("2026-03-10", "2026-03-11", "Costco",           first_id,  184.22, "expense"),
        ("2026-03-18", "2026-03-19", "Uber",             second_id, 24.50,  "expense"),
    ]

    batch = db.execute(
        "INSERT INTO import_batches (filename, account_id, row_count, dupes_count) VALUES (?,?,?,?)",
        ("test_seed.csv", first_id, len(fake_rows), 0)
    ).lastrowid

    for trx_d, post_d, vendor, acct_id, amt_val, typ in fake_rows:
        key_raw = f"{post_d}|{vendor.upper()}|{amt_val:.2f}"
        dkey    = hashlib.md5(key_raw.encode()).hexdigest()
        db.execute("""
            INSERT OR IGNORE INTO staging
              (import_batch_id, account_id, raw_trx_date, raw_post_date, raw_description,
               raw_amount, trx_date, post_date, vendor, amount, trx_type, dedup_key, status, owner)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (batch, acct_id, trx_d, post_d, vendor,
              -amt_val, trx_d, post_d, vendor, amt_val, typ, dkey, "pending", OWNER))

    db.commit()
    flash(f"Seeded {len(fake_rows)} fake transactions into the review queue.", "success")
    return redirect(url_for("review_queue"))


def register(app, helpers):
    """Bind this module's routes. Endpoint names == function names,
    exactly as in the pre-split app.py."""
    login_required = helpers["login_required"]
    global import_csv, review_queue, review_approve, review_restore, \
        review_hard_delete, review_delete_all_discarded, trash_view, \
        trx_restore, trx_purge, staging_purge, undo_import_batch, review_skip, \
        review_discard, review_update, import_manual, admin_seed_review, amt
    amt = helpers["amt"]
    import_csv = login_required(import_csv)
    app.route("/import", methods=["GET", "POST"])(import_csv)
    review_queue = login_required(review_queue)
    app.route("/review")(review_queue)
    review_approve = login_required(review_approve)
    app.route("/api/review/<int:staging_id>/approve", methods=["POST"])(review_approve)
    review_restore = login_required(review_restore)
    app.route("/api/review/<int:staging_id>/restore", methods=["POST"])(review_restore)
    review_hard_delete = login_required(review_hard_delete)
    app.route("/api/review/<int:staging_id>/delete", methods=["POST"])(review_hard_delete)
    review_delete_all_discarded = login_required(review_delete_all_discarded)
    app.route("/api/review/discarded/delete-all", methods=["POST"])(review_delete_all_discarded)
    trash_view = login_required(trash_view)
    app.route("/trash")(trash_view)
    trx_restore = login_required(trx_restore)
    app.route("/api/transactions/<int:trx_id>/restore", methods=["POST"])(trx_restore)
    trx_purge = login_required(trx_purge)
    app.route("/api/transactions/<int:trx_id>/purge", methods=["POST"])(trx_purge)
    staging_purge = login_required(staging_purge)
    app.route("/api/staging/<int:staging_id>/purge", methods=["POST"])(staging_purge)
    undo_import_batch = login_required(undo_import_batch)
    app.route("/api/import-batch/<int:batch_id>/undo", methods=["POST"])(undo_import_batch)
    review_skip = login_required(review_skip)
    app.route("/api/review/<int:staging_id>/skip", methods=["POST"])(review_skip)
    review_discard = login_required(review_discard)
    app.route("/api/review/<int:staging_id>/discard", methods=["POST"])(review_discard)
    review_update = login_required(review_update)
    app.route("/api/review/<int:staging_id>/update", methods=["POST"])(review_update)
    import_manual = login_required(import_manual)
    app.route("/import/manual", methods=["GET", "POST"])(import_manual)
    admin_seed_review = login_required(admin_seed_review)
    app.route("/admin/seed-review", methods=["POST"])(admin_seed_review)
