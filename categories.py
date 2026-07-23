"""
categories.py — the starter L1/L2 category trees + account seeds.

These are the seeds init_db() writes into the categories/accounts tables
on FIRST RUN ONLY. Once your database exists, edit categories from
Docs & Settings (or ask your AI assistant — see docs/customize_with_ai.md).

Customizing before first run: edit the tuples below. Each entry is
(L1, L2) — L1 is the broad group, L2 the specific bucket. Keep at least
one L2 under every L1.
"""

EXPENSE_CATS = [
    ("Housing", "Rent / Mortgage"), ("Housing", "Utilities"),
    ("Housing", "Internet & Phone"), ("Housing", "Furnishings"),
    ("Housing", "Repairs & Maintenance"), ("Housing", "Insurance"),
    ("Food & Dining", "Groceries"), ("Food & Dining", "Restaurants"),
    ("Food & Dining", "Coffee & Takeout"), ("Food & Dining", "Bars"),
    ("Transportation", "Gas"), ("Transportation", "Public Transit"),
    ("Transportation", "Rideshare & Taxi"), ("Transportation", "Parking & Tolls"),
    ("Transportation", "Car Payment"), ("Transportation", "Car Insurance"),
    ("Transportation", "Repairs & Maintenance"),
    ("Health & Fitness", "Medical"), ("Health & Fitness", "Dental & Vision"),
    ("Health & Fitness", "Pharmacy"), ("Health & Fitness", "Gym & Fitness"),
    ("Health & Fitness", "Insurance Premiums"),
    ("Shopping", "Clothing"), ("Shopping", "Electronics"),
    ("Shopping", "Home Goods"), ("Shopping", "Gifts"),
    ("Shopping", "Miscellaneous"),
    ("Entertainment", "Streaming & Subscriptions"), ("Entertainment", "Events & Activities"),
    ("Entertainment", "Hobbies"), ("Entertainment", "Miscellaneous"),
    ("Travel", "Airfare"), ("Travel", "Lodging"),
    ("Travel", "Ground Transportation"), ("Travel", "Meals"),
    ("Travel", "Miscellaneous"),
    ("Personal Care", "Hair & Beauty"), ("Personal Care", "Dry Cleaning & Laundry"),
    ("Education", "Tuition & Courses"), ("Education", "Books & Supplies"),
    ("Miscellaneous", "Fees & Charges"), ("Miscellaneous", "Miscellaneous"),
]

INCOME_CATS = [
    ("Salary & Wages", "Primary Job"), ("Salary & Wages", "Side Income"),
    ("Interest & Dividends", "Interest"), ("Interest & Dividends", "Dividends"),
    ("Miscellaneous", "Refunds & Reimbursements"), ("Miscellaneous", "Gifts Received"),
    ("Miscellaneous", "Other"),
    # Taxes live in the income tree as contra-income (negative amounts):
    # withheld-from-paycheck taxes and direct payments both land here, so
    # gross income and after-tax income are both visible.
    ("Taxes", "Federal Income Tax"), ("Taxes", "Social Security"),
    ("Taxes", "Medicare"), ("Taxes", "State Income Tax"),
    # Used by the Payroll tool: a combined FICA line (when a paystub shows
    # one number), other payroll taxes (PFML / SDI / local), and pre-tax
    # health/other paycheck deductions — all contra-income, like Taxes
    # above. (Pre-tax RETIREMENT is not here: it books as a transfer —
    # see TRANSFER_CATS below and routes_payroll.py.)
    ("Taxes", "FICA (SS + Medicare)"), ("Taxes", "Other Payroll Tax"),
    ("Pre-Tax Deductions", "Health & Other Benefits"),
    # Post-tax paycheck deductions (garnishments, union dues, etc.) — money
    # you earned that was taken after tax. Contra-income, NOT a tax: it books
    # here so gross income stays truthful. (Post-tax ROTH retirement is not
    # here: like pre-tax retirement it books as a transfer — your own money
    # moving into your own account.)
    ("Post-Tax Deductions", "Garnishments & Other"),
]

# Starter accounts — placeholders so first launch has something to import
# into. Rename/add your real accounts (ask your AI assistant, or edit here
# BEFORE first run). Each entry is (name, last-4 or handle, type, owner).
ACCOUNTS = [
    ("My Checking",    "0001",  "checking",       "ME"),
    ("My Credit Card", "0002",  "credit_card",    "ME"),
    ("Venmo",          "venmo", "digital_wallet", "ME"),
]

# ── Investment accounts (EXPERIMENTAL module) ────────────────────────────────
# Each is (name, handle, type, owner, provider, opening_balance, l1)
# `l1` mirrors the L1 transfer category that funds this account.
# Empty by default — add accounts from the Investments page in the app.
INVESTMENT_ACCOUNTS = []

# ── Transfer categories ──────────────────────────────────────────────────────
# L1/L2 pairs for trx_type='transfer'. L2 names match investment account
# names — the app maintains these automatically as you add investment
# accounts. The three L1 groups are the canonical investment groupings.
# "Retirement Plan" is the Payroll tool's generic fallback for pre-tax
# retirement transfers when no Investments account is targeted (the
# Investments Sync engine only pulls transfers whose L2 names a real
# investment account, so this one stays books-only).
TRANSFER_CATS = [
    ("Retirement", "Retirement Plan"),
]
