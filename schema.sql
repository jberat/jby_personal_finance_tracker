-- ============================================================
-- Personal Financial Tracker — Schema
-- ============================================================

PRAGMA foreign_keys = ON;

-- ============================================================
-- ACCOUNTS
-- One row per financial account (card, checking, etc.)
-- ============================================================
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,           -- e.g. "My Checking"
    account_num TEXT,                    -- last 4, e.g. "0001"
    type        TEXT NOT NULL,           -- credit_card | checking | savings
    owner       TEXT NOT NULL DEFAULT 'ME',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- CATEGORIES
-- L1 + L2 pairs. trx_type: expense | income
-- ============================================================
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trx_type    TEXT NOT NULL,           -- expense | income
    l1          TEXT NOT NULL,
    l2          TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(trx_type, l1, l2)
);

-- ============================================================
-- (STATEMENT PERIODS — retired.)
-- Billing cycles now live ON the accounts table: stmt_close_day +
-- pay_due_day columns (added by init_db's idempotent ALTERs), set from
-- Docs & Settings → Accounts. billing.py holds the date math.
-- ============================================================

-- ============================================================
-- TAGS
-- User-defined project/event tags (e.g. "Wedding 2026")
-- ============================================================
CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    color       TEXT DEFAULT '#6B7280',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- IMPORT BATCHES
-- One row per CSV upload event
-- ============================================================
CREATE TABLE IF NOT EXISTS import_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT NOT NULL,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    row_count    INTEGER,
    dupes_count  INTEGER DEFAULT 0,
    imported_at  TEXT NOT NULL DEFAULT (datetime('now')),
    note         TEXT
);

-- ============================================================
-- STAGING
-- Transactions waiting for review. Cleared once approved/discarded.
-- status: pending | approved | skipped | discarded
-- ============================================================
CREATE TABLE IF NOT EXISTS staging (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    import_batch_id INTEGER NOT NULL REFERENCES import_batches(id),
    account_id      INTEGER NOT NULL REFERENCES accounts(id),

    -- Raw fields from import (immutable)
    raw_trx_date    TEXT NOT NULL,
    raw_post_date   TEXT,
    raw_description TEXT NOT NULL,
    raw_amount      REAL NOT NULL,     -- as-imported (may be negative)
    raw_category    TEXT,              -- Chase's own category label
    raw_type        TEXT,              -- Chase type field

    -- Derived / editable pre-approval
    trx_date        TEXT NOT NULL,
    post_date       TEXT,
    statement_date  TEXT,
    vendor          TEXT,
    amount          REAL NOT NULL,     -- normalized; sign matches transactions table convention
    trx_type        TEXT DEFAULT 'expense',  -- expense | income | transfer
    l1_category     TEXT,
    l2_category     TEXT,
    note            TEXT,
    receipt_path    TEXT,              -- preserved through review-queue reroutes

    -- Dedup fingerprint
    dedup_key       TEXT NOT NULL,     -- concat of raw_trx_date|raw_description|raw_amount

    status          TEXT NOT NULL DEFAULT 'pending',
    reviewed_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_staging_status ON staging(status);
CREATE INDEX IF NOT EXISTS idx_staging_batch  ON staging(import_batch_id);

-- ============================================================
-- TRANSACTIONS
-- Approved, locked transactions.
-- ============================================================
CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    staging_id      INTEGER REFERENCES staging(id),   -- source staging row

    -- Dates
    trx_date        TEXT NOT NULL,
    post_date       TEXT,
    statement_date  TEXT,
    payment_date    TEXT,

    -- Description / vendor
    raw_description TEXT NOT NULL,    -- original from import, never changes
    vendor          TEXT,             -- cleaned vendor name
    description     TEXT,             -- optional user note on vendor

    -- Amount & type
    -- amount sign convention (Option A):
    --   trx_type='expense', amount > 0 → normal expense (money out)
    --   trx_type='expense', amount < 0 → credit / contra-expense (money in offsetting an expense)
    --   trx_type='income',  amount > 0 → true income
    -- Importer uses a $500 threshold to default cash-in to credit vs income.
    amount          REAL NOT NULL,
    trx_type        TEXT NOT NULL DEFAULT 'expense',  -- expense | income | transfer
    owner           TEXT NOT NULL DEFAULT 'ME',       -- single-user build: always 'ME'

    -- Categorization
    l1_category     TEXT,
    l2_category     TEXT,

    -- Extras
    note            TEXT,
    receipt_path    TEXT,

    -- Split / link support
    parent_id       INTEGER REFERENCES transactions(id),  -- if this is a child split
    is_split        INTEGER NOT NULL DEFAULT 0,           -- 1 if this trx has children
    link_id         INTEGER REFERENCES transactions(id),  -- linked trx (e.g. refund ↔ charge)

    -- Status
    status          TEXT NOT NULL DEFAULT 'active',   -- active | deleted
    dedup_key       TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trx_date     ON transactions(trx_date);
CREATE INDEX IF NOT EXISTS idx_trx_type     ON transactions(trx_type);
CREATE INDEX IF NOT EXISTS idx_trx_account  ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_trx_parent   ON transactions(parent_id);
CREATE INDEX IF NOT EXISTS idx_trx_dedup    ON transactions(dedup_key);

-- ============================================================
-- TRANSACTION TAGS (many-to-many)
-- ============================================================
CREATE TABLE IF NOT EXISTS transaction_tags (
    trx_id  INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (trx_id, tag_id)
);

-- ============================================================
-- INVESTMENT ADJUSTMENTS
-- One-sided events on investment accounts (no cash flow on the
-- checking side). Captures unrealized gains/losses, dividends,
-- interest credits, fees, etc.
--
-- value(account) = opening_balance
--                  + Σ transfers_in (cash-flow trxs, negated for inflow)
--                  + Σ amount in this table for the account
-- invested(account) = opening_balance + Σ transfers_in (excluding adjustments)
-- ============================================================
CREATE TABLE IF NOT EXISTS investment_adjustments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    adj_date    TEXT NOT NULL,                                -- YYYY-MM-DD
    amount      REAL NOT NULL,                                -- + = gain, − = loss
    kind        TEXT NOT NULL DEFAULT 'unrealized',           -- unrealized | dividend | interest | fee | other
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_inv_adj_account ON investment_adjustments(account_id);
CREATE INDEX IF NOT EXISTS idx_inv_adj_date    ON investment_adjustments(adj_date);

-- ============================================================
-- TRANSACTION LINKS (many-to-many, symmetric)
-- Each row links two transactions. Stored canonically with a_id < b_id
-- so every relationship is a single row regardless of which side
-- created it. Replaces the legacy `transactions.link_id` column —
-- supports multiple links per transaction.
-- ============================================================
CREATE TABLE IF NOT EXISTS transaction_links (
    a_id        INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    b_id        INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (a_id, b_id),
    CHECK (a_id < b_id)
);
CREATE INDEX IF NOT EXISTS idx_trx_links_a ON transaction_links(a_id);
CREATE INDEX IF NOT EXISTS idx_trx_links_b ON transaction_links(b_id);

-- ============================================================
-- KNOWN DEDUP KEYS
-- Union of staging + transactions dedup keys for fast dupe detection
-- Maintained via trigger
-- ============================================================
CREATE TABLE IF NOT EXISTS known_dedup_keys (
    dedup_key   TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL
);

-- Triggers to keep known_dedup_keys in sync
CREATE TRIGGER IF NOT EXISTS trg_staging_insert
AFTER INSERT ON staging
BEGIN
    INSERT OR IGNORE INTO known_dedup_keys(dedup_key, account_id)
    VALUES (NEW.dedup_key, NEW.account_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_trx_insert
AFTER INSERT ON transactions
BEGIN
    INSERT OR IGNORE INTO known_dedup_keys(dedup_key, account_id)
    VALUES (NEW.dedup_key, NEW.account_id);
END;

-- Auto-update updated_at on transactions
CREATE TRIGGER IF NOT EXISTS trg_trx_updated_at
AFTER UPDATE ON transactions
BEGIN
    UPDATE transactions SET updated_at = datetime('now') WHERE id = NEW.id;
END;
