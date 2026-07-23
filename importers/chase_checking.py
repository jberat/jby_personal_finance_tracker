"""
Chase Checking CSV importer.

Chase Checking columns:
    Details, Posting Date, Description, Amount, Type, Check or Slip #

Sign convention (NEW — staging preserves raw bank flow):
- staging.amount = raw_amount (matches bank: + = money in, − = money out)
- staging.trx_type is determined by Chase's Type field + a $500 threshold
  promotion for ambiguous large cash-in
- review_approve flips the sign for expense-type rows when committing to
  transactions (so transactions.amount follows the post-approval
  semantic: + = expense outflow, − = contra-expense; income/transfer
  keep their raw signs)

The $500 threshold only fires when Chase's Type signal is ambiguous;
explicit income signals always honor classify's verdict.
"""

# True-income / contra-expense threshold for cash-in transactions
INCOME_THRESHOLD = 500.0

import csv
import hashlib
from datetime import datetime

try:
    from vendor_rules import clean_vendor, strip_noise, skip_reason, frag_in
except ImportError:  # ensure repo root is importable when run as a subpackage
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from vendor_rules import clean_vendor, strip_noise, skip_reason, frag_in
from dedup_utils import find_duplicate, parse_amount


VENDOR_MAP = {
    # Order matters — first match wins. Keep specific patterns BEFORE generic
    # ones. Add your own fragments here, e.g.:
    # "CARCO FIN":     "Car Lease",
    # "529 PLAN":      "College Savings",
    "VENMO":          "Venmo",
    "PAYPAL":         "PayPal",
    "IRS":            "IRS",
    "AMAZON":         "Amazon",
    "AMZN":           "Amazon",
    "APPLE":          "Apple",
    "GOOGLE":         "Google",
    "NETFLIX":        "Netflix",
    "SPOTIFY":        "Spotify",
    # ZELLE handled specially below — extract counterparty name from description.
}


def _extract_zelle_counterparty(desc_up):
    """Pull the person/entity name out of a Zelle description.
    e.g., 'Zelle Payment To Jane Doe Jpm99XYZ' → 'Jane Doe'."""
    for marker in ("ZELLE PAYMENT TO ", "ZELLE PAYMENT FROM ",
                   "ZELLE ONLINE PAYMENT TO ", "ZELLE ONLINE PAYMENT FROM "):
        if marker in desc_up:
            tail = desc_up.split(marker, 1)[1]
            # Trim at known boundary markers (Chase appends ref numbers after the name)
            for stop in (" JPM", " WEB ID", " ACH", " ID:", " WF "):
                if stop in tail:
                    tail = tail.split(stop, 1)[0]
                    break
            # Also trim trailing 4+ digit sequences (raw ref numbers — Chase
            # appends them after the name; real human names don't have these).
            import re
            tail = re.sub(r"\s+\d{4,}.*$", "", tail)
            tail = tail.strip()
            return tail.title() if tail else None
    return None


def normalize_vendor(desc: str, raw_amount=None) -> str:
    # Shared cross-importer rules first (single source of truth).
    shared = clean_vendor(desc, raw_amount=raw_amount, is_cc=False)
    if shared:
        return shared
    desc_up = desc.upper()
    # Zelle: pull counterparty from the description
    if "ZELLE" in desc_up:
        cp = _extract_zelle_counterparty(desc_up)
        if cp:
            return cp
        return "Zelle"  # fallback if extraction fails
    # Direct map (first match wins). (2026-07-03 fix: word-boundary matching
    # via frag_in — bare substring made "IRS" hit FIRSTBANK, "APPLE" hit
    # APPLEBEES, etc.)
    for fragment, name in VENDOR_MAP.items():
        if frag_in(fragment, desc_up):
            return name
    # Default: title-case the raw description, then drop store/ref numbers
    return strip_noise(desc.strip().title())


# ── Type classification by Chase "Type" field ────────────────────────────────
TYPE_MAP = {
    "ACH_DEBIT":       "expense",
    "ACH_CREDIT":      "income",
    "QUICKPAY_CREDIT": "income",   # Zelle received
    "QUICKPAY_DEBIT":  "expense",  # Zelle sent
    "ACCT_XFER":       "transfer",
    "CHECK":           "expense",
    "ATM":             "expense",
    "FEE_TRANSACTION": "expense",
    "WIRE_OUTGOING":   "expense",
    "WIRE_INCOMING":   "income",
    "DEPOSIT":         "income",
}

# ── Patterns to auto-flag as 'ignore' (recommend discard during review) ─────
# These are imported separately via the CC / Venmo / PayPal flows and must not
# be double-counted in checking. Discard cleanly during review.
IGNORE_PATTERNS = [
    "ONLINE PAYMENT",
    "PAYMENT THANK YOU",
    "AUTOMATIC PAYMENT",
    "AUTOPAY",
    "AUTO-PAY",
    "AUTO PAY",
    "CHASE CREDIT",    # Chase-to-Chase CC payments
    "VENMO",
    # PayPal deliberately absent — those rows go to manual review, not auto-discard
]

# ── Patterns to auto-flag as 'transfer' (NOT discarded — they're internal
# movements to record but keep neutral on P&L). E.g., manual broker
# deposits, account-to-account, IRA contributions, brokerage sweeps.
TRANSFER_PATTERNS = [
    "ACCT XFER",
    "MANUAL DB-BKRG",
    "DB-BKRG",
]


def is_ignorable(description: str) -> bool:
    desc_up = description.upper()
    return any(p in desc_up for p in IGNORE_PATTERNS)


# Map an auto-ignored row to a human-readable flag reason for the review queue.
_CC_PAYMENT_HINTS = ("PAYMENT THANK YOU", "AUTOMATIC PAYMENT", "AUTOPAY",
                     "AUTO-PAY", "AUTO PAY", "ONLINE PAYMENT", "CHASE CREDIT")
def ignore_reason(description: str) -> str:
    du = description.upper()
    if "VENMO" in du:
        return "Venmo"
    if any(h in du for h in _CC_PAYMENT_HINTS):
        return "CC payment"
    return "Auto-ignored"


def is_transfer(description: str, chase_type: str) -> bool:
    desc_up = description.upper()
    if chase_type.upper() == "ACCT_XFER":
        return True
    return any(p in desc_up for p in TRANSFER_PATTERNS)


def classify(details: str, chase_type: str, amount: float, description: str = ""):
    """Return trx_type for a checking row."""
    # CC payments, Venmo, PayPal → recommend discard during review
    if is_ignorable(description):
        return "ignore"
    # Internal transfers / broker movements → keep but neutral
    if is_transfer(description, chase_type):
        return "transfer"

    t = TYPE_MAP.get(chase_type.upper(), None)
    if t:
        return t
    # Fall back to Details field
    if details.upper() == "CREDIT":
        return "income"
    return "expense"


def make_dedup_key(posting_date: str, description: str, amount: float) -> str:
    raw = f"{posting_date}|{description.upper().strip()}|{amount:.2f}"
    return hashlib.md5(raw.encode()).hexdigest()


def parse_date(s: str) -> str:
    """Parse MM/DD/YYYY → YYYY-MM-DD."""
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return s.strip()


def parse(filepath: str, account_id: int, db):
    """
    Parse a Chase Checking CSV. Returns (rows_to_insert, dupe_count, skipped),
    where `skipped` is a list of {reason, sample} for rows that couldn't be
    parsed — so the caller reports WHY instead of a mute "file appears empty".
    """
    rows  = []
    dupes = 0
    skipped = []

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        # Wrong-file guard: a Chase CHECKING export has a "Posting Date" column.
        # If it's absent, this is almost certainly a credit-card (or other) file
        # imported against a checking account — the #1 cause of a mute failure.
        if "Posting Date" not in fieldnames:
            skipped.append({"reason": "header", "expected": "Posting Date",
                            "kind": "checking",
                            "sample": ",".join(fieldnames)[:70] or "(no header)"})
        for raw in reader:
            details      = raw.get("Details", "").strip()
            posting_date = raw.get("Posting Date", "").strip()
            description  = raw.get("Description", "").strip()
            amount_raw   = raw.get("Amount", "0").strip()
            chase_type   = raw.get("Type", "").strip()

            if not posting_date or not description:
                skipped.append({"reason": "missing_date_or_desc",
                                "sample": (posting_date or description or "(blank)")[:40]})
                continue

            raw_amount = parse_amount(amount_raw)
            if raw_amount is None:
                skipped.append({"reason": "amount", "sample": amount_raw[:20]})
                continue

            post_date = parse_date(posting_date)

            # Checking has only a posting date; use it as trx_date too
            trx_date  = post_date

            dedup_key = make_dedup_key(posting_date, description, raw_amount)
            dup = find_duplicate(db, account_id, trx_date, description, raw_amount)
            is_dupe = dup is not None
            dup_of_trx_id = dup["id"] if (dup and dup["kind"] == "transaction") else None
            if is_dupe:
                dupes += 1

            trx_type = classify(details, chase_type, raw_amount, description)

            # CC-bill payments / Venmo / autopay → skip on import (segregated
            # like duplicates, never auto-imported). classify() returns
            # 'ignore' for these; convert to a sane trx_type so a restored row
            # is still usable, and mark is_skip for the staging status.
            is_skip = (trx_type == "ignore") or (skip_reason(description) is not None)
            if trx_type == "ignore":
                trx_type = "transfer" if is_transfer(description, chase_type) else "expense"

            # ── Checking-side card-payment rows are KEPT ─────────────────────
            # The money-out row that pays a credit card imports as a KEPT
            # 'Credit Card Payment' transfer (not skipped) — it feeds the
            # Reconcile Card tool, which ties the payment to the exact charges
            # it settled. L2 names the card: defaulted when exactly one active
            # credit-card account exists, otherwise picked in the review
            # queue. Only the checking side (money leaving, raw_amount < 0)
            # is kept; card-side 'Payment Thank You' rows stay skipped
            # (they'd double-count).
            cc_l1, cc_l2 = None, None
            if ignore_reason(description) == "CC payment" and raw_amount < 0:
                trx_type = "transfer"
                is_skip = False
                cc_l1 = "Credit Card Payment"
                _cc_accts = db.execute(
                    "SELECT name FROM accounts WHERE type='credit_card' "
                    "AND active=1").fetchall()
                cc_l2 = _cc_accts[0]["name"] if len(_cc_accts) == 1 else None

            # Flag reason for the review queue (duplicate wins, then skip class).
            if is_dupe:
                flag_reason = "Duplicate"
            elif cc_l1:
                flag_reason = "CC payment"
            elif is_skip:
                flag_reason = ignore_reason(description)
            elif trx_type == "transfer":
                flag_reason = "Internal transfer"
            else:
                flag_reason = None

            # Threshold rule for ambiguous cash-in classified as expense:
            # promote to income if it's larger than the threshold (likely a
            # real income deposit, not a small refund/contra-expense).
            if trx_type == "expense" and raw_amount > 0 and abs(raw_amount) > INCOME_THRESHOLD:
                trx_type = "income"

            # Staging preserves raw bank sign — review queue shows it as-is.
            # The approve handler flips the sign for expense-type rows
            # when committing to transactions.
            amount = raw_amount

            rows.append({
                "raw_trx_date":    posting_date,
                "raw_post_date":   posting_date,
                "raw_description": description,
                "raw_amount":      raw_amount,
                "raw_category":    None,
                "raw_type":        chase_type or None,
                "trx_date":        trx_date,
                "post_date":       post_date,
                # Card-payment transfers get the card itself as vendor —
                # "who was paid" is the card, not the raw ACH descriptor.
                "vendor":          ((cc_l2 or normalize_vendor(description))
                                    if cc_l1 else normalize_vendor(description)),
                "amount":          amount,
                "trx_type":        trx_type,
                "l1_category":     cc_l1,  # 'Credit Card Payment' for card-pmt rows; else manual
                "l2_category":     cc_l2,
                "dedup_key":       dedup_key,
                "is_dupe":         is_dupe,
                "is_skip":         is_skip,
                "dup_of_trx_id":   dup_of_trx_id,
                "flag_reason":     flag_reason,
            })

    return rows, dupes, skipped
