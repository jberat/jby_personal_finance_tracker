"""
reset_db.py — Wipe all transaction data from the Personal Financial
Tracker database (finance.db, path read from config.py).

Clears: transactions (all, including parent/child splits and linked rows),
        staging, import_batches, transaction_tags, known_dedup_keys,
        receipts, transaction_links.
Preserves: accounts, categories, tags.

Always writes a timestamped .bak.<UTC> backup of the DB before deleting.

Run from the app directory:
    python reset_db.py

Add --yes to skip the confirmation prompt:
    python reset_db.py --yes
"""
import sys
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

DB_PATH = Path(config.DB_PATH)

# Order matters: clear children before parents to avoid FK noise even
# though FK enforcement is also disabled for safety with the self-FK on
# transactions (parent_id, link_id).
# (2026-07-03 fix: receipts + transaction_links added. The old list left
# receipts rows holding matched_trx_id values that — combined with an
# autoincrement reset — silently re-pointed at whatever NEW transactions
# later inherited the old ids.)
WIPE_TABLES = [
    "transaction_tags",
    "transaction_links",
    "receipts",
    "transactions",
    "staging",
    "import_batches",
    "known_dedup_keys",
]

PRESERVE_TABLES = [
    "accounts",
    "categories",
    "tags",
]


def _count(db, table):
    try:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return "N/A"


def reset(yes: bool = False):
    if not DB_PATH.exists():
        print(f"[reset_db] No database found at {DB_PATH}. Nothing to do.")
        return

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    print("\n[reset_db] Current row counts:")
    for t in WIPE_TABLES:
        print(f"  WIPE     {t:<22} {_count(db, t)}")
    for t in PRESERVE_TABLES:
        print(f"  PRESERVE {t:<22} {_count(db, t)}")

    if not yes:
        confirm = input(
            "\n  This will permanently delete the WIPE tables above.\n"
            "  PRESERVE tables stay untouched.\n"
            "  A timestamped backup will be made first.\n\n"
            "  Type 'yes' to continue: "
        ).strip().lower()
        if confirm != "yes":
            print("Aborted.")
            db.close()
            return

    # Backup
    db.close()  # release file before copy for safety
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = DB_PATH.with_suffix(f".db.bak.{ts}")
    shutil.copy2(DB_PATH, backup_path)
    print(f"\n[reset_db] Backup written → {backup_path.name}")

    # Wipe
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = OFF;")
    for table in WIPE_TABLES:
        try:
            db.execute(f"DELETE FROM {table}")
        except Exception as e:
            print(f"  Warning: could not clear {table}: {e}")
    # (2026-07-03: autoincrement is deliberately NOT reset. Reusing old ids
    # means anything that survived a wipe — or any stale reference in docs,
    # filed receipt names, exports, git history — silently points at the
    # WRONG new row. Gapless ids aren't worth that class of bug.)
    db.commit()
    # NOTE: no VACUUM — it requires an exclusive lock the running app
    # holds. The deletes alone are enough; SQLite reuses freed pages.

    print("\n[reset_db] Post-wipe counts:")
    for t in WIPE_TABLES:
        print(f"  WIPE     {t:<22} {_count(db, t)}")
    for t in PRESERVE_TABLES:
        print(f"  PRESERVE {t:<22} {_count(db, t)}")

    db.close()
    print("\n[reset_db] Done. Ready for fresh imports.\n")


if __name__ == "__main__":
    reset(yes="--yes" in sys.argv)
