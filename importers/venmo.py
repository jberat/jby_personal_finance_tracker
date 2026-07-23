"""
Venmo statement CSV importer.

Venmo's "Download CSV" statement export has a few preamble rows before the
real header. The header row contains (columns vary slightly by year —
matching is by name, not position):

    ,ID,Datetime,Type,Status,Note,From,To,Amount (total),Amount (tip),
    Amount (tax),Amount (fee),Tax Rate,Tax Exempt,Funding Source,
    Destination,Beginning Balance,Ending Balance, ...

Conventions:
- Amount (total) is signed from YOUR wallet's point of view and formatted
  like "- $25.00" / "+ $1,200.00". Positive = money into your Venmo.
- Type ∈ Payment / Charge / Standard Transfer / Instant Transfer /
  Deposit / Merchant Transaction / ... Transfers and deposits move money
  between Venmo and your bank → trx_type='transfer' (neutral on P&L,
  and the bank side is skipped by the checking importer's Venmo filter,
  so nothing double-counts).
- Vendor = the counterparty: `To` when you paid, `From` when you were
  paid. The Venmo note (memo) is carried into the staging note.
- Only Status == 'Complete' rows import normally; anything else is
  flagged for review (segregated like duplicates, never auto-imported).
- Sign convention matches the other importers: staging preserves the raw
  wallet sign; the approve handler flips signs for expense-type rows.
  Cash-in defaults to contra-expense; > $500 promotes to income
  (same threshold rule as checking).
"""

INCOME_THRESHOLD = 500.0

import csv
import hashlib
import re
from datetime import datetime

try:
    from vendor_rules import strip_noise
except ImportError:  # ensure repo root is importable when run as a subpackage
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from vendor_rules import strip_noise
from dedup_utils import find_duplicate, parse_amount


# Types that move money between Venmo and your bank account.
TRANSFER_TYPES = {"standard transfer", "instant transfer", "deposit"}


def _parse_amount(s: str):
    """'- $25.00' / '+ $1,200.00' / '$3.50' → signed float, or None.
    Delegates to the shared tolerant parser (dedup_utils.parse_amount), which
    also absorbs parenthesized negatives, trailing minus, and NBSPs from
    hand-edited exports."""
    return parse_amount(s)


def _parse_date(s: str) -> str:
    """'2026-01-05T12:34:56' → '2026-01-05' (tolerates plain dates too)."""
    s = (s or "").strip()
    if "T" in s:
        return s.split("T", 1)[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s[:10]


def _find_header(filepath):
    """Return (fieldnames, data_start_line). Venmo files start with a
    preamble; the real header is the row containing ID + Datetime +
    an Amount column."""
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        for i, line in enumerate(csv.reader(f)):
            cells = [c.strip() for c in line]
            if "ID" in cells and "Datetime" in cells and any(
                    c.startswith("Amount") for c in cells):
                return line, i
    return None, None


def make_dedup_key(venmo_id: str, date: str, description: str, amount: float) -> str:
    """Venmo's transaction ID is globally unique — key on it when present."""
    if venmo_id:
        raw = f"venmo|{venmo_id}"
    else:
        raw = f"{date}|{description.upper().strip()}|{amount:.2f}"
    return hashlib.md5(raw.encode()).hexdigest()


def parse(filepath: str, account_id: int, db):
    """Parse a Venmo statement CSV. Returns (rows_to_insert, dupe_count,
    skipped) — `skipped` lists {reason, sample} entries so the caller can
    report WHY nothing imported (wrong file, garbled amounts) instead of a
    mute "file appears empty"."""
    header, start = _find_header(filepath)
    if header is None:
        # Wrong-file guard: a Venmo statement export always has a header row
        # with ID + Datetime + an Amount column. Missing → this isn't a
        # Venmo export (likely a bank CSV imported against the Venmo account).
        return [], 0, [{"reason": "header", "expected": "ID, Datetime, Amount",
                        "kind": "Venmo statement",
                        "sample": "(no Venmo header row found)"}]

    rows = []
    dupes = 0
    skipped = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for _ in range(start + 1):
            next(reader, None)
        for line in reader:
            raw = {h.strip(): (line[i].strip() if i < len(line) else "")
                   for i, h in enumerate(header)}

            venmo_id = raw.get("ID", "")
            dt = raw.get("Datetime", "")
            vtype = raw.get("Type", "")
            status = raw.get("Status", "")
            note = raw.get("Note", "")
            p_from = raw.get("From", "")
            p_to = raw.get("To", "")
            amount = _parse_amount(raw.get("Amount (total)", ""))

            # Preamble/balance/disclaimer rows have no ID or no amount —
            # those skip silently (structural, present in every export). A
            # real transaction row (has an ID) with an unparseable amount is
            # reported loudly.
            if not venmo_id or not dt:
                continue
            if amount is None:
                skipped.append({"reason": "amount",
                                "sample": raw.get("Amount (total)", "")[:20]})
                continue

            trx_date = _parse_date(dt)

            # Counterparty: To when money left your wallet, From when it
            # arrived — EXCEPT Charge rows, where Venmo puts the REQUESTER in
            # From: a charge YOU made (+) lists you as From and the payer as
            # To, so the rule inverts for Charge rows.
            # Transfers show your bank as the destination.
            if vtype.lower() == "charge":
                counterparty = (p_to if amount > 0 else p_from) or p_to or p_from
            else:
                counterparty = (p_to if amount < 0 else p_from) or p_from or p_to
            vendor = strip_noise((counterparty or "Venmo").strip().title())

            # Deterministic raw description (feeds dedup + review display).
            arrow = "to" if amount < 0 else "from"
            description = f"Venmo {vtype} {arrow} {counterparty}".strip()
            if note:
                description += f": {note}"

            dedup_key = make_dedup_key(venmo_id, trx_date, description, amount)
            dup = find_duplicate(db, account_id, trx_date, description, amount)
            is_dupe = dup is not None
            dup_of_trx_id = dup["id"] if (dup and dup["kind"] == "transaction") else None
            if is_dupe:
                dupes += 1

            # Type: bank transfers/deposits are neutral transfers; everything
            # else is spend (out) or contra-expense/income (in).
            if vtype.lower() in TRANSFER_TYPES:
                trx_type = "transfer"
            elif amount < 0:
                trx_type = "expense"
            else:
                trx_type = ("income" if abs(amount) > INCOME_THRESHOLD
                            else "expense")   # small cash-in = contra-expense

            # Non-complete rows (Issued, Pending, Failed…) never auto-import.
            is_skip = status.lower() not in ("complete", "completed", "")
            if is_dupe:
                flag_reason = "Duplicate"
            elif is_skip:
                flag_reason = f"Venmo status: {status}"
            elif trx_type == "transfer":
                flag_reason = "Internal transfer"
            else:
                flag_reason = None

            rows.append({
                "raw_trx_date":    dt,
                "raw_post_date":   None,
                "raw_description": description,
                "raw_amount":      amount,
                "raw_category":    None,
                "raw_type":        vtype or None,
                "trx_date":        trx_date,
                "post_date":       trx_date,
                "vendor":          vendor,
                "amount":          amount,
                "trx_type":        trx_type,
                "l1_category":     None,   # person-to-person — categorize in review
                "l2_category":     None,
                "note":            note or None,
                "dedup_key":       dedup_key,
                "is_dupe":         is_dupe,
                "is_skip":         is_skip,
                "dup_of_trx_id":   dup_of_trx_id,
                "flag_reason":     flag_reason,
            })

    return rows, dupes, skipped
