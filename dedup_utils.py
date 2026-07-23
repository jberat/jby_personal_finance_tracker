"""
dedup_utils.py — authoritative duplicate detection.

Shared by both importers and app.py. Replaces the fragile reliance on the
known_dedup_keys cache (which desynced when a transaction was edited or
moved) AND on the raw dedup_key string (which broke when Chase changed its
CSV date format between exports — e.g. "4/1/2026" vs "04/01/2026" — hashing
the same transaction to different keys).

Instead we match on EXPORT-STABLE, NORMALIZED fields that both the importer
and the stored rows have in a consistent form:
    account_id + normalized trx_date (YYYY-MM-DD) + raw description + |amount|

Chase exports the same transaction with a byte-identical raw description
and amount every time; only the date *format* varies, which normalizing
the parsed trx_date fully absorbs. |amount| is used
so the post-approval sign flip on transactions (expense stored positive)
doesn't cause a miss.

A row is a DUPLICATE if a prior copy exists as:
  - an active transaction, or
  - a split PARENT (status='deleted' AND is_split=1) — the original of
    something already split in the app (shared bills, mixed carts, etc.), or
  - an in-flight staging row (pending / duplicate).

It is NOT a duplicate of a user-trashed transaction (status='deleted' AND
is_split=0) — those were deliberately removed, so a delete-then-reimport
comes in clean. Rows within the SAME import are never matched against each
other (they aren't in the DB yet at parse time), so legitimate same-day /
same-amount repeats (e.g. multiple subway swipes) are preserved.
"""


def parse_amount(s):
    """Tolerant amount parser for hand-prepared / Excel-exported CSVs.

    Hand-edited bank exports often carry currency-formatted amounts —
    "$1,234.56", "1,234.56", "(456.78)" (accounting-style negatives), a
    trailing "-", or stray whitespace / NBSPs. The importers used bare
    float(), which threw on all of these and (under `except ValueError:
    continue`) silently dropped every row → the misleading "file appears
    empty" error. This strips currency symbols + thousands commas, treats
    "(x)" and a trailing "-" as negative, and returns None on true garbage
    so the caller can COUNT + report the skip instead of hiding it.

    For an already-clean numeric string it returns exactly float(s), so
    dedup keys (which hash the parsed float) are byte-identical on clean
    files — zero behavior change for the normal path."""
    if s is None:
        return None
    t = str(s).strip()
    if t == "":
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg = True
        t = t[1:-1].strip()
    t = (t.replace(",", "").replace("$", "").replace("€", "")
          .replace("£", "").replace(" ", "").strip())
    if t.endswith("-"):          # rare trailing-minus style
        neg = True
        t = t[:-1].strip()
    try:
        v = float(t)
    except (ValueError, TypeError):
        return None
    return -v if neg else v


def _norm_desc(s):
    # Space-insensitive (2026-07-03 hardening): Chase pads descriptions with
    # variable runs of spaces between exports ("VENDOR    0000…"). Exact
    # whitespace matching re-broke dedup the same way the old date-format
    # dependence did. Removing all spaces on BOTH sides absorbs it; any rare
    # over-flag lands in review, where a human dismisses it.
    return (s or "").upper().replace(" ", "")


def find_duplicate(db, account_id, trx_date, raw_description, raw_amount,
                   exclude_staging_id=None, exclude_trx_id=None):
    """Return info about the existing row this transaction duplicates, or None.

    Match key: account_id + trx_date (normalized YYYY-MM-DD) + raw_description
    + round(abs(amount), 2). db must yield sqlite3.Row.

    Returns {kind:'transaction'|'staging', id, vendor, amount, owner, status,
    trx_date, is_split} or None. Transactions win over staging.
    """
    desc = _norm_desc(raw_description)
    amt = round(abs(float(raw_amount)), 2)

    tq = """
        SELECT id, vendor, amount, owner, status, trx_date,
               COALESCE(is_split, 0) AS is_split
          FROM transactions
         WHERE account_id = ? AND trx_date = ?
           AND REPLACE(UPPER(COALESCE(raw_description, '')), ' ', '') = ?
           AND ROUND(ABS(amount), 2) = ?
           AND (status = 'active'
                OR (status = 'deleted' AND COALESCE(is_split, 0) = 1))
    """
    tparams = [account_id, trx_date, desc, amt]
    if exclude_trx_id is not None:
        tq += " AND id != ?"
        tparams.append(exclude_trx_id)
    tq += " ORDER BY (status = 'active') DESC, id ASC LIMIT 1"
    t = db.execute(tq, tparams).fetchone()
    if t:
        return {
            "kind": "transaction", "id": t["id"], "vendor": t["vendor"],
            "amount": t["amount"], "owner": t["owner"], "status": t["status"],
            "trx_date": t["trx_date"], "is_split": t["is_split"],
        }

    sq = """
        SELECT id, vendor, amount, owner, status, trx_date
          FROM staging
         WHERE account_id = ? AND trx_date = ?
           AND REPLACE(UPPER(COALESCE(raw_description, '')), ' ', '') = ?
           AND ROUND(ABS(amount), 2) = ?
           AND status IN ('pending', 'duplicate')
    """
    sparams = [account_id, trx_date, desc, amt]
    if exclude_staging_id is not None:
        sq += " AND id != ?"
        sparams.append(exclude_staging_id)
    sq += " ORDER BY id ASC LIMIT 1"
    s = db.execute(sq, sparams).fetchone()
    if s:
        return {
            "kind": "staging", "id": s["id"], "vendor": s["vendor"],
            "amount": s["amount"], "owner": s["owner"], "status": s["status"],
            "trx_date": s["trx_date"], "is_split": 0,
        }
    return None
