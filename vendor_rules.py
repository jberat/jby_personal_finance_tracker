"""
vendor_rules.py — single source of truth for vendor-name cleanup.

Shared by both importers (chase_cc, chase_checking), the review-queue
rules in app.py, and the one-time backfill migration. Keeping the rules
here means a vendor correction is written once and applies everywhere a
vendor name is derived: on import, in the review queue, and to existing
rows via backfill.

Public API:
    clean_vendor(desc, raw_amount=None, is_cc=False) -> str | None
        Returns a corrected vendor name if any shared rule matches,
        else None (caller keeps its own derived name).

Design notes:
- Deterministic renames are case-insensitive substring matches.
- The "Chase Credit" rule is the only amount-aware rule: it fires only
  for credit-card inflows (is_cc=True, raw_amount > 0) that are NOT card
  payments and whose description carries a recurring "$XX/period" marker
  (e.g. "$10/month", "$300/year"). Those are automatic statement credits
  (subscription statement credits, travel credits, etc.). Real merchant
  refunds lack the period marker, so they keep their merchant name.
"""

import re

# Recurring "$XX/period" marker, e.g. "$10/month", "$300 / yr", "$25/mo".
_PERIOD_RE = re.compile(
    r"\$\s?\d+(?:\.\d{1,2})?\s?/\s?"
    r"(?:mo|mos|month|months|wk|week|weeks|yr|yrs|year|years|day|days|qtr|quarter)\b",
    re.IGNORECASE,
)

# CC *payment* descriptions (you paying the card) — never "Chase Credit".
_PAYMENT_RE = re.compile(
    r"payment\s+thank|autopay|automatic\s+payment|online\s+payment|e-?pay|"
    r"\bpymt\b|payment\s+received",
    re.IGNORECASE,
)


def _title(s: str) -> str:
    return s.strip().title()


def clean_vendor(desc, raw_amount=None, is_cc=False):
    """Return a corrected vendor name, or None if no shared rule applies.

    desc       : raw bank description (or an already-derived vendor name).
    raw_amount : as-imported amount (bank sign: + = money in). Needed only
                 for the Chase Credit rule. May be None.
    is_cc      : True when called from the credit-card importer. Gates the
                 Chase Credit rule (checking inflows are income, not credits).
    """
    if not desc:
        return None
    up = desc.upper()

    # 1. Automatic CC statement credit → "Chase Credit".
    #    Non-payment credit-card inflow carrying a "$XX/period" marker.
    if (is_cc and raw_amount is not None and raw_amount > 0
            and not _PAYMENT_RE.search(desc)
            and _PERIOD_RE.search(desc)):
        return "Chase Credit"

    # 2. Deterministic renames (case-insensitive substring). Add your own
    #    here — one `if` per merchant whose bank description doesn't clean
    #    up well on its own.
    if "STARBUCKS" in up:                  # covers "STARBUCKS STORE #1234"
        return "Starbucks"
    if "AMZN MKTP" in up:                  # covers "AMZN Mktp US*XX0XX0XX0"
        return "Amazon"

    # 2b. Airline truncations — Chase truncates / glues a ref number to the
    #     carrier name (e.g. "SOUTHWES 0000000000000",
    #     "AMERICAN AIR0000000000"). Match the stable prefix. These also
    #     drive the Travel/Airfare category override (see is_airline()).
    if "SOUTHWES" in up:                   # covers SOUTHWES / SOUTHWEST
        return "Southwest Airlines"
    if "AMERICAN AIR" in up:
        return "American Airlines"

    # 3. Strip a leading Toast "tst*" prefix, keep the real merchant name.
    if up.startswith("TST*") or up.startswith("TST *"):
        rest = desc.split("*", 1)[1] if "*" in desc else ""
        return strip_noise(_title(rest)) if rest.strip() else "Toast"

    return None


# ── Skip detection (card-bill payments) ──────────────────────────────────────
# Card-payment detection. The two sides of a card payment are treated
# DIFFERENTLY: the CHECKING-side money-out row is KEPT and typed as a
# 'Credit Card Payment' transfer (it feeds the Reconcile Card tool); the
# CARD-side 'Payment Thank You' row is flagged 'skip' (the underlying
# purchases import per-transaction from the card's own CSV, so keeping
# the payment row too would double-count). See chase_checking.py for the
# keep path and chase_cc.py for the skip path.
_SKIP_RE = re.compile(
    r"payment\s+thank|autopay|auto\s*-?\s*pay|automatic\s+payment|"
    r"online\s+payment|e-?pay|\bpymt\b|payment\s+received|chase\s+credit\s+crd",
    re.IGNORECASE,
)


def skip_reason(desc):
    """Return a short reason string if this row should be skipped on import
    (card-bill payment), else None. The string is also the review-queue
    pill label."""
    if not desc:
        return None
    if _SKIP_RE.search(desc):
        return "CC payment"
    return None


# ── Airline detection (Travel / Airfare category override) ───────────────────
def is_airline(vendor):
    """True when a (cleaned) vendor name is an airline — drives the
    Travel/Airfare auto-category in the review rules."""
    if not vendor:
        return False
    v = vendor.lower()
    return "airline" in v or "air lines" in v


# ── Generic noise stripping ──────────────────────────────────────────────────
# Applied by the importers to the FINAL derived vendor name (the title-cased
# fallback path), after the deterministic rules above. Removes store numbers
# and ref/phone digit-strings that vary per transaction, so the same merchant
# collapses to one vendor name.
_HASH_NUM_RE  = re.compile(r"#\s*\d+")              # "#307", "# 44126"
_DIGIT_RUN_RE = re.compile(r"\b\d[\d\-]{2,}\b")     # 3+ char digit/phone runs
_POSS_RE      = re.compile(r"'S\b")                 # "Domino'S" -> "Domino's"
_EDGE_SEP_RE  = re.compile(r"^\s*[\-–—*]\s*|\s*[\-–—*]\s*$")


def strip_noise(name):
    """Drop store numbers (#307), ref/phone digit-strings, and tidy casing.

    Keeps short brand numbers (e.g. "76", "7-Eleven") by only removing digit
    runs of length >= 3. Idempotent and safe to apply to already-clean names.
    """
    if not name:
        return name
    s = _HASH_NUM_RE.sub(" ", name)
    s = _DIGIT_RUN_RE.sub(" ", s)
    s = _POSS_RE.sub("'s", s)
    s = _EDGE_SEP_RE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s or name


# ── VENDOR_MAP fragment matching (2026-07-03) ────────────────────────────────
# Shared by both importers. Word-boundary matching for plain fragments —
# bare substring matching made "APPLE" hit APPLEBEES, "IRS" hit FIRSTBANK,
# "UBER" hit HUBER, etc. Fragments containing punctuation ("SQ *",
# "APPLE.COM", "WAL-MART") keep substring semantics. `exclude` handles the
# whole-word false positives boundaries can't ("UNITED" in UNITED HEALTHCARE,
# "DELTA" in DELTA DENTAL).

def frag_in(fragment: str, desc_up: str, exclude: tuple = ()) -> bool:
    """True if VENDOR_MAP `fragment` matches `desc_up` (both uppercase)."""
    if any(x in desc_up for x in exclude):
        return False
    frag = fragment.upper()
    if re.search(r"[^A-Z0-9 ]", frag):          # punctuation → substring
        return frag in desc_up
    return re.search(r"(?<![A-Z0-9])" + re.escape(frag) + r"(?![A-Z0-9])",
                     desc_up) is not None
