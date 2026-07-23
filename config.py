"""
config.py — every constant + secret loader for Personal Financial Tracker.

Everything the app needs to know about YOUR machine lives here. The
defaults are designed to work out of the box: the database, imports
folder, and receipts folders are all created inside the app folder on
first run. See docs/install_guide.md.
"""
import os
from datetime import datetime

CURRENT_YEAR = str(datetime.now().year)


# ─── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE_DIR, "finance.db")
SCHEMA    = os.path.join(BASE_DIR, "schema.sql")

# Raw CSV uploads are archived here on every import, sorted by account type,
# with a UTC timestamp prefix so the same filename can land twice without
# clobbering.
IMPORTS_ROOT          = os.path.join(BASE_DIR, "imports")
IMPORTS_CHECKING_DIR  = os.path.join(IMPORTS_ROOT, "checking")
IMPORTS_CC_DIR        = os.path.join(IMPORTS_ROOT, "creditcards")
IMPORTS_OTHER_DIR     = os.path.join(IMPORTS_ROOT, "other")
# Payroll CSVs (Gusto Payroll Journal Report exports) auto-save here on
# upload so the Payroll tool keeps a permanent import history.
IMPORTS_PAYROLL_DIR   = os.path.join(IMPORTS_ROOT, "payroll")

# Receipts pipeline folders. Drop receipt files (PDF/JPG/PNG/HEIC) into the
# inbox; matched receipts are filed into receipts/filed/<YYYY>/<L1>/<L2>/<MM>/.
RECEIPTS_ROOT  = os.path.join(BASE_DIR, "receipts")
RECEIPTS_INBOX = os.path.join(RECEIPTS_ROOT, "inbox")

# Single-user build: every transaction belongs to the one owner 'ME'.
# (The DB keeps an owner column so multi-entity forks stay possible.)
OWNER = "ME"
RECEIPTS_OWNER_FOLDER = {OWNER: "filed"}


def _owner_to_receipts_folder(owner: str) -> str:
    """Single-portal build: everything files under receipts/filed/."""
    return RECEIPTS_OWNER_FOLDER.get(owner, "filed")


# Path normalization hook (legacy naming). Kept as an extension point for
# installs that need to translate stored paths; this build stores paths
# exactly as it sees them.
MAC_RECEIPTS_ROOT = os.path.dirname(BASE_DIR)


def to_mac_path(p) -> str:
    """Identity in this build — paths are stored as-is."""
    return str(p)


def _read_local_secret(fname):
    """Read a single-line secret from a gitignored file next to app.py.
    Returns None if the file doesn't exist / is empty."""
    try:
        with open(os.path.join(BASE_DIR, fname)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _get_or_create_secret_key():
    """Load the Flask session secret from .secret_key (gitignored), creating
    a random one on first run. A hardcoded/committed secret would let anyone
    who has read the repo forge session cookies."""
    existing = _read_local_secret(".secret_key")
    if existing:
        return existing
    import secrets as _secrets
    key = _secrets.token_hex(32)
    try:
        p = os.path.join(BASE_DIR, ".secret_key")
        with open(p, "w") as f:
            f.write(key)
        os.chmod(p, 0o600)
    except OSError:
        pass  # worst case: sessions reset on restart
    return key


# Credentials: env var → gitignored local file → first-run default.
# CHANGE THE PASSWORD: `echo 'yournewpassword' > .app_password` in the app
# folder, then restart. The default only exists so first launch works.
APP_USER     = os.environ.get("APP_USER") or _read_local_secret(".app_user") or "me"
APP_PASSWORD = os.environ.get("APP_PASSWORD") or _read_local_secret(".app_password") or "changeme"
SECRET_KEY   = os.environ.get("SECRET_KEY") or _get_or_create_secret_key()


# Keyboard shortcuts for the receipts review screen (editable in
# Docs & Settings → Shortcuts).
SHORTCUT_ACTIONS = {
    "confirm":     "Confirm & file (advance to next)",
    "skip":        "Skip receipt",
    "not_receipt": "Not a receipt",
    "repick":      "Not this match (search)",
    "link":        "Link to another transaction",
    "split":       "Open split panel",
    "discard":     "Discard duplicate receipt",
}
DEFAULT_SHORTCUTS = {
    "confirm": "Enter", "skip": "s", "not_receipt": "n",
    "repick": "r", "link": "l", "split": "x", "discard": "d",
}


# ─── Tools registry ──────────────────────────────────────────────────────────
# Single source of truth for every tool: the "All Tools" page, the Tools
# submenu (pick which show in Docs & Settings → Tools Menu), and nav
# active-state all read from here. Add new tools HERE, not in base.html.
TOOLS_REGISTRY = [
    {"key": "import", "title": "Import", "built": True,
     "portals": ("ME",),
     "url": {"ME": "/tools/import"},
     "eps": ["tools_import", "import_csv", "import_manual"],
     "desc": "Upload bank CSVs (checking / credit card / Venmo) into the "
             "review queue, or add a transaction manually.",
     "note": "Safe to re-import overlapping date ranges — duplicates flag "
             "automatically."},
    {"key": "tax_estimator", "title": "Tax Estimator", "built": True,
     "portals": ("ME",),
     "url": {"ME": "/tools/tax-estimator"},
     "eps": ["tools_tax_estimator"],
     "desc": "Walkthrough wizard that estimates 2026 federal + state income "
             "tax from a handful of numbers. Stateless — nothing is saved.",
     "note": "Estimates only — not tax advice; state math is approximate."},
    {"key": "payroll", "title": "Payroll", "built": True,
     "portals": ("ME",),
     "url": {"ME": "/tools/payroll"},
     "eps": ["tools_payroll", "tools_payroll_upload", "tools_payroll_manual",
             "tools_payroll_view"],
     "desc": "Reconcile paychecks so the books show GROSS income: import a "
             "Gusto payroll CSV or type numbers off any paystub, then split "
             "the imported net deposit into gross wages, taxes withheld, "
             "and pre-tax deductions.",
     "note": "Everything is preview-first and undoable; a YTD mode handles "
             "annual true-ups without per-paycheck data."},
    {"key": "ccrecon", "title": "Reconcile Card", "built": True,
     "portals": ("ME",),
     "url": {"ME": "/tools/reconcile-card"},
     "eps": ["tools_reconcile_card", "tools_reconcile_card_detail"],
     "desc": "Close the card cycle: tie each card payment to the exact "
             "charges it settled, prove the math to the penny, and true-up "
             "payment + statement dates.",
     "note": "Run once per month after the autopay imports. Preview-first; "
             "unwindable."},
    {"key": "receipts", "title": "Receipts", "built": True,
     "portals": ("ME",),
     "url": {"ME": "/tools/receipts"},
     "eps": ["tools_receipts", "receipts_orphans", "receipts_review"],
     "desc": "Scan the inbox, review matches one at a time, confirm to file. "
             "Orphans view for anything unmatched.",
     "note": "Keyboard shortcuts customizable in Docs & Settings → Shortcuts."},
    {"key": "cleanup", "title": "Data Cleanup", "built": True,
     "portals": ("ME",),
     "url": {"ME": "/tools/cleanup"},
     "eps": ["tools_cleanup"],
     "desc": "Vendor names, tag merges, missing dates, duplicate suspects, "
             "category problems — reviewed and fixed one item at a time.",
     "note": "Re-scans on every load; Ignore hides a finding forever."},
    {"key": "actuals_vs_budget", "title": "Actuals vs. Budget", "built": True,
     "portals": ("ME",),
     "url": {"ME": "/tools/actuals-vs-budget"},
     "eps": ["tools_actuals_vs_budget"],
     "desc": "Spend per category vs the budget targets set in Docs & "
             "Settings → Assumptions → Budget Values.",
     "note": None},
]
DEFAULT_TOOLS_MENU = ["import", "ccrecon", "tax_estimator", "payroll", "receipts"]


# FX rates (units per USD), used by the receipts matcher for foreign-currency
# receipts (±10% band). Edit in Docs & Settings → Assumptions → Exchange Rates.
FX_RATES = {
    "EUR": {"key": "fx_eur_per_usd", "default": "0.92", "symbol": "€",
            "label": "Euro (EUR)"},
    "GBP": {"key": "fx_gbp_per_usd", "default": "0.79", "symbol": "£",
            "label": "British Pound (GBP)"},
}


_ALLOWED_ORIGINS = {"http://127.0.0.1:5005", "http://localhost:5005"}


# Credit-card billing cycles are DB-driven: each credit-card account
# stores its own statement close day (+ optional payment due day) on the
# accounts table, editable from Docs & Settings → Accounts. See billing.py
# for the statement/payment date math. (The old static CC_BILLING_CYCLES
# dict is retired — any close days it once held now live on the accounts.)
