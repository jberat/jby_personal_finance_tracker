"""
Chase Credit Card CSV importer.

Chase CC columns:
    Transaction Date, Post Date, Description, Category, Type, Amount, Memo

Amount sign convention (Option A — sign-on-amount):
- Purchases (raw negative)     → trx_type='expense', amount > 0
- Cash-in (raw positive)       → trx_type='expense', amount < 0  (credit / contra-expense)

Cash-in on a CC is treated as contra-expense by default. CCs don't
normally receive true income; on the rare exception (e.g. a manual
deposit reward), re-type in the review queue before approving.
"""

import csv
import hashlib
import re
from datetime import datetime

try:
    from vendor_rules import clean_vendor, strip_noise, skip_reason, frag_in
except ImportError:  # ensure repo root is importable when run as a subpackage
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from vendor_rules import clean_vendor, strip_noise, skip_reason, frag_in
from dedup_utils import find_duplicate, parse_amount
from billing import calc_dates_for_close


# ── Vendor normalization map ─────────────────────────────────────────────────
# Raw CC description fragment → clean vendor name
VENDOR_MAP = {
    "APPLE.COM":       "Apple",
    "AMAZON":          "Amazon",
    "AMZN":            "Amazon",
    "GOOGLE":          "Google",      # catches GOOGLE *ONE, GOOGLE *WORKSPACE_…
    "WALMART":         "Walmart",
    "WAL-MART":        "Walmart",
    "TARGET":          "Target",
    "COSTCO":          "Costco",
    "WHOLEFDS":        "Whole Foods",
    "WHOLE FOODS":     "Whole Foods",
    "TRADER JOE":      "Trader Joe's",
    "NETFLIX":         "Netflix",
    "SPOTIFY":         "Spotify",
    "HULU":            "Hulu",
    "HBO":             "HBO Max",
    "DOORDASH":        "DoorDash",
    "UBER EATS":       "Uber Eats",
    "GRUBHUB":         "Grubhub",
    "UBER":            "Uber",
    "LYFT":            "Lyft",
    "AIRBNB":          "Airbnb",
    "DELTA":           "Delta Air Lines",
    "UNITED":          "United Airlines",
    "AMERICAN AIR":    "American Airlines",
    "SOUTHWEST":       "Southwest Airlines",
    "MARRIOTT":        "Marriott",
    "HILTON":          "Hilton",
    "HYATT":           "Hyatt",
    "SQ *":            None,   # Square merchant — keep description
    "TST*":            None,   # Toast merchant
    # Add your own fragments here, e.g.:
    # "CARCO FIN": "Car Lease",
}

# Whole-word false positives that word boundaries can't catch (2026-07-03):
# "UNITED" is a whole word inside UNITED HEALTHCARE; "DELTA" inside DELTA
# DENTAL. Both then cascaded via is_airline() → Travel/Airfare on backfill.
VENDOR_MAP_EXCLUDE = {
    "DELTA":  ("DENTAL", "FAUCET"),
    "UNITED": ("HEALTH",),
}


def normalize_vendor(desc: str, raw_amount=None, is_cc=True) -> str:
    # Shared cross-importer rules first (single source of truth).
    shared = clean_vendor(desc, raw_amount=raw_amount, is_cc=is_cc)
    if shared:
        return shared
    desc_up = desc.upper()
    for fragment, name in VENDOR_MAP.items():
        # (2026-07-03 fix: word-boundary matching via frag_in — bare
        # substring made "UBER" hit HUBER, "DELTA" hit DELTA DENTAL, etc.)
        if frag_in(fragment, desc_up, VENDOR_MAP_EXCLUDE.get(fragment, ())):
            if name:
                return name
            # For None entries (SQ */TST*), strip the processor prefix then
            # clean up the raw description a bit
            cleaned = re.sub(r"^\s*(SQ\s*\*|TST\*)\s*", "", desc, flags=re.I)
            return strip_noise(cleaned.title())
    # Default: title-case the raw description, then drop store/ref numbers
    return strip_noise(desc.strip().title())


# ── Category mapping (Chase category → the app's L1/L2) ─────────────────────
# Chase provides their own category — use as a hint only. Targets are the
# starter tree in categories.py; if you customize your categories, update
# these pairs to match (everything unmapped just lands in manual review).
CATEGORY_MAP = {
    "Shopping":       ("Shopping",         "Miscellaneous"),
    "Groceries":      ("Food & Dining",    "Groceries"),
    "Food & Drink":   ("Food & Dining",    "Restaurants"),
    "Dining":         ("Food & Dining",    "Restaurants"),
    "Travel":         ("Travel",           "Miscellaneous"),
    "Gas":            ("Transportation",   "Gas"),
    "Health & Wellness": ("Health & Fitness", "Medical"),
    "Entertainment":  ("Entertainment",    "Events & Activities"),
    "Automotive":     ("Transportation",   "Repairs & Maintenance"),
    "Home":           ("Housing",          "Repairs & Maintenance"),
    "Utilities":      ("Housing",          "Utilities"),
    "Insurance":      ("Health & Fitness", "Insurance Premiums"),
    "Personal":       ("Miscellaneous",    "Miscellaneous"),
    "Education":      ("Education",        "Tuition & Courses"),
    "Fees & Adjustments": ("Miscellaneous","Fees & Charges"),
    "Gifts & Donations": ("Shopping",      "Gifts"),
    "Transfers":      None,   # will become transfer type
    "Payments":       None,
    "Other Travel":   ("Travel",           "Miscellaneous"),
}


def classify(chase_category: str, desc_upper: str):
    """Return (trx_type, l1, l2) based on Chase category + description."""
    cat = (chase_category or "").strip()
    mapped = CATEGORY_MAP.get(cat)

    if mapped is None and cat in ("Transfers", "Payments"):
        return "transfer", None, None

    if mapped:
        return "expense", mapped[0], mapped[1]

    # Unrecognized — leave for manual review
    return "expense", None, None


def make_dedup_key(trx_date: str, description: str, amount: float) -> str:
    raw = f"{trx_date}|{description.upper().strip()}|{amount:.2f}"
    return hashlib.md5(raw.encode()).hexdigest()


def parse_date(s: str) -> str:
    """Parse MM/DD/YYYY → YYYY-MM-DD."""
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return s.strip()


def parse(filepath: str, account_id: int, db):
    """
    Parse a Chase CC CSV. Returns (rows_to_insert, dupe_count, skipped),
    where `skipped` is a list of {reason, sample} for unparseable rows so the
    caller can report WHY (currency-formatted amounts, wrong header, etc.)
    instead of a mute "file appears empty".
    """
    rows   = []
    dupes  = 0
    skipped = []

    # Statement-date assignment: read the card's billing days from its
    # account row (stmt_close_day is set on Docs & Settings → Accounts).
    # Every imported row gets its statement_date computed from post date
    # (trx-date fallback) at import time; without a close day it stays
    # blank and the review queue / wizard fall back to post dates.
    _acct = db.execute(
        "SELECT type, stmt_close_day, pay_due_day FROM accounts WHERE id=?",
        (account_id,)).fetchone()
    close_day = _acct["stmt_close_day"] if _acct else None
    due_day   = _acct["pay_due_day"] if _acct else None

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        # Wrong-file guard: a Chase CREDIT-CARD export has a "Transaction Date"
        # column (checking uses "Posting Date"). If it's absent, this is a
        # checking/other file imported against a credit-card account — the #1
        # cause of a mute "file appears empty".
        if "Transaction Date" not in fieldnames:
            skipped.append({"reason": "header", "expected": "Transaction Date",
                            "kind": "credit-card",
                            "sample": ",".join(fieldnames)[:70] or "(no header)"})
        for raw in reader:
            trx_date_raw  = raw.get("Transaction Date", "").strip()
            post_date_raw = raw.get("Post Date", "").strip()
            description   = raw.get("Description", "").strip()
            chase_cat     = raw.get("Category", "").strip()
            trx_type_raw  = raw.get("Type", "").strip()
            amount_raw    = raw.get("Amount", "0").strip()
            memo          = raw.get("Memo", "").strip()

            if not trx_date_raw or not description:
                skipped.append({"reason": "missing_date_or_desc",
                                "sample": (trx_date_raw or description or "(blank)")[:40]})
                continue

            raw_amount = parse_amount(amount_raw)
            if raw_amount is None:
                skipped.append({"reason": "amount", "sample": amount_raw[:20]})
                continue

            trx_date  = parse_date(trx_date_raw)
            post_date = parse_date(post_date_raw) if post_date_raw else None

            # Dedup check
            dedup_key = make_dedup_key(trx_date_raw, description, raw_amount)
            dup = find_duplicate(db, account_id, trx_date, description, raw_amount)
            is_dupe = dup is not None
            dup_of_trx_id = dup["id"] if (dup and dup["kind"] == "transaction") else None
            if is_dupe:
                dupes += 1

            # Card-bill payment (you paying the CC) → skip on import. These
            # appear on the CC statement as "AUTOMATIC PAYMENT", "PAYMENT
            # THANK YOU", etc. They are never imported as transactions —
            # segregated like duplicates for the user to confirm.
            skip_flag = skip_reason(description)
            is_skip = skip_flag is not None

            # Determine direction from Chase's category hint
            trx_type, l1, l2 = classify(chase_cat, description.upper())

            # Cash-in on a CC is essentially always a refund / contra-expense
            # (true income to a CC is vanishingly rare). Default
            # everything cash-in to type='expense' with negative amount;
            # user re-types in the review queue on the very rare exception.
            if raw_amount > 0:
                trx_type = "expense"   # contra-expense / credit (negative on approve)
                l1, l2   = None, None

            # Staging preserves raw bank sign — review queue shows it as-is.
            # CC purchases come through negative (money out); refunds/payments
            # positive. The approve handler flips signs for expense-type rows.
            amount = raw_amount

            # Statement date from the account's close day (post-date basis;
            # trx-date fallback when Chase omits the post date). Interest
            # charges, fees, refunds — everything buckets the same way.
            statement_date, _ = calc_dates_for_close(
                post_date or trx_date, close_day, due_day)

            rows.append({
                "raw_trx_date":    trx_date_raw,
                "raw_post_date":   post_date_raw or None,
                "raw_description": description,
                "raw_amount":      raw_amount,
                "raw_category":    chase_cat or None,
                "raw_type":        trx_type_raw or None,
                "trx_date":        trx_date,
                "post_date":       post_date,
                "statement_date":  statement_date,
                "vendor":          normalize_vendor(description, raw_amount=raw_amount, is_cc=True),
                "amount":          amount,
                "trx_type":        trx_type,
                "l1_category":     l1,
                "l2_category":     l2,
                "dedup_key":       dedup_key,
                "is_dupe":         is_dupe,
                "is_skip":         is_skip,
                "dup_of_trx_id":   dup_of_trx_id,
                "flag_reason":     ("Duplicate" if is_dupe
                                    else (skip_flag if is_skip else None)),
            })

    return rows, dupes, skipped
