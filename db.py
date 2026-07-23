"""
db.py — data layer: connection, app_settings store, schema init/migrations.

init_db() runs on every start and is fully idempotent: it creates the
schema on first run, applies additive migrations on upgrades, and reseeds
the category dropdowns from categories.py (categories added in the app
live in custom_categories and survive the reseed).

get_db()/init_db() read config.DB_PATH dynamically so tests can point the
whole app at a throwaway DB copy by setting config.DB_PATH.
"""
import os
import sqlite3
from flask import g

import config
from config import SCHEMA, OWNER
from categories import (EXPENSE_CATS, INCOME_CATS, ACCOUNTS,
                        INVESTMENT_ACCOUNTS, TRANSFER_CATS)


# ─── DB helpers ───────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(config.DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


# ─── App settings (key-value) ────────────────────────────────────────────────
# Small shared settings store: FX rates, keyboard shortcuts, etc. Ensured
# lazily so it exists regardless of how the app was started.

def _ensure_app_settings(db):
    db.execute("""CREATE TABLE IF NOT EXISTS app_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')))""")


def get_setting(db, key, default=None):
    _ensure_app_settings(db)
    row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(db, key, value):
    _ensure_app_settings(db)
    db.execute("""INSERT INTO app_settings (key, value, updated_at)
                  VALUES (?, ?, datetime('now'))
                  ON CONFLICT(key) DO UPDATE SET
                      value=excluded.value, updated_at=excluded.updated_at""",
               (key, str(value)))


def init_db():
    db = sqlite3.connect(config.DB_PATH)
    db.row_factory = sqlite3.Row

    with open(SCHEMA) as f:
        db.executescript(f.read())

    # ── Additive columns on staging ──────────────────────────────────────────
    for _ddl in (
        "ALTER TABLE staging ADD COLUMN dup_of_trx_id INTEGER",
        "ALTER TABLE staging ADD COLUMN flag_reason TEXT",
        "ALTER TABLE staging ADD COLUMN reroute_src_trx_id INTEGER",
    ):
        try:
            db.execute(_ddl)
        except Exception:
            pass  # column already exists

    # ── Clean up any duplicate accounts (no UNIQUE constraint on the table) ──
    # Rows WITHOUT an account_num are excluded: the number is optional on
    # accounts added via Docs & Settings → Accounts, and SQLite GROUP BY
    # buckets NULLs together — without the guard, two numberless accounts
    # would be treated as duplicates and one deleted on every boot.
    dupes = db.execute("""
        SELECT account_num FROM accounts
        WHERE account_num IS NOT NULL AND account_num != ''
        GROUP BY account_num HAVING COUNT(*) > 1
    """).fetchall()
    for row in dupes:
        num = row["account_num"]
        keeper = db.execute(
            "SELECT MIN(id) as id FROM accounts WHERE account_num=?", (num,)
        ).fetchone()["id"]
        db.execute(
            "DELETE FROM accounts WHERE account_num=? AND id != ?", (num, keeper)
        )

    # ── Schema migrations on accounts table (idempotent ALTERs) ──────────────
    for sql in (
        "ALTER TABLE accounts ADD COLUMN provider TEXT",
        "ALTER TABLE accounts ADD COLUMN opening_balance REAL DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN notes TEXT",
        "ALTER TABLE accounts ADD COLUMN opened_date TEXT",
        "ALTER TABLE accounts ADD COLUMN closed_date TEXT",
        # Investment accounts carry a user-editable L1 group
        # (Retirement / General Savings / Alternatives).
        "ALTER TABLE accounts ADD COLUMN inv_group TEXT",
        # Credit-card billing days (Docs & Settings → Accounts):
        # stmt_close_day (1–31, required for credit_card adds) drives
        # statement-date assignment + the Reconcile Card wizard;
        # pay_due_day (optional) drives payment-date estimates.
        "ALTER TABLE accounts ADD COLUMN stmt_close_day INTEGER",
        "ALTER TABLE accounts ADD COLUMN pay_due_day INTEGER",
    ):
        try: db.execute(sql)
        except Exception: pass  # column already exists

    # ── Retire the legacy statement_periods table ────────────────────────────
    # Billing cycles are now stored on the accounts themselves. One-time
    # migration: copy each card's close/due day onto its account row (only
    # where unset), then drop the old table.
    try:
        for r in db.execute(
                "SELECT account_id, close_day, due_day FROM statement_periods"
        ).fetchall():
            db.execute(
                """UPDATE accounts SET
                       stmt_close_day = COALESCE(stmt_close_day, ?),
                       pay_due_day    = COALESCE(pay_due_day, ?)
                   WHERE id=?""",
                (r["close_day"], r["due_day"], r["account_id"]))
        db.execute("DROP TABLE statement_periods")
    except Exception:
        pass  # table already gone (fresh DBs never create it)

    # ── Seed accounts — check by account_num to avoid dupes ──────────────────
    for name, num, typ, owner in ACCOUNTS:
        exists = db.execute(
            "SELECT id FROM accounts WHERE account_num=?", (num,)
        ).fetchone()
        if not exists:
            db.execute(
                "INSERT INTO accounts (name, account_num, type, owner) VALUES (?,?,?,?)",
                (name, num, typ, owner)
            )

    # ── Seed investment accounts (provider + opening_balance preserved) ──────
    for name, num, typ, owner, provider, opening, _l1 in INVESTMENT_ACCOUNTS:
        existing = db.execute(
            "SELECT id, opening_balance FROM accounts WHERE account_num=?", (num,)
        ).fetchone()
        if existing is None:
            db.execute("""INSERT INTO accounts
                (name, account_num, type, owner, provider, opening_balance, inv_group)
                VALUES (?,?,?,?,?,?,?)""",
                (name, num, typ, owner, provider, opening, _l1))
        else:
            # Preserve in-app edits: once an account exists, name/provider are
            # the user's. Only re-assert type + owner each boot; inv_group
            # backfills only where unset.
            db.execute("UPDATE accounts SET type=?, owner=? WHERE id=?",
                       (typ, OWNER, existing["id"]))
            db.execute("""UPDATE accounts SET inv_group=?
                WHERE id=? AND (inv_group IS NULL OR inv_group='')""",
                (_l1, existing["id"]))

    # ── Seed categories ───────────────────────────────────────────────────────
    # categories.py is the source-of-truth for the STARTER tree. Wipe and
    # re-seed so code-side edits propagate; transactions store category
    # strings as TEXT (not FK), so this never breaks existing rows — it just
    # refreshes the dropdown options. Categories added inside the app live in
    # custom_categories and are re-applied right after.
    db.execute("DELETE FROM categories WHERE trx_type IN ('expense','income','transfer')")
    for l1, l2 in EXPENSE_CATS:
        db.execute("INSERT INTO categories (trx_type, l1, l2) VALUES ('expense',?,?)", (l1, l2))
    for l1, l2 in INCOME_CATS:
        db.execute("INSERT INTO categories (trx_type, l1, l2) VALUES ('income',?,?)", (l1, l2))
    for l1, l2 in TRANSFER_CATS:
        db.execute("INSERT INTO categories (trx_type, l1, l2) VALUES ('transfer',?,?)", (l1, l2))

    db.execute("""CREATE TABLE IF NOT EXISTS custom_categories (
        trx_type   TEXT NOT NULL,
        l1         TEXT NOT NULL,
        l2         TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(trx_type, l1, l2))""")
    db.execute("""INSERT OR IGNORE INTO categories (trx_type, l1, l2)
                  SELECT trx_type, l1, l2 FROM custom_categories""")

    # ── Card-payment transfer categories (maintained, like investments) ──────
    # Every credit-card account gets a ('Credit Card Payment', <card name>)
    # transfer category — the canonical signal that a transfer is a card
    # payment (the checking importer assigns it; the review queue and the
    # Reconcile Card wizard key on it; display forks 'CC Payment' over it).
    # Re-derived from the accounts table each boot, so renames done in the
    # app stay in sync after the category reseed above.
    for r in db.execute(
            "SELECT name FROM accounts WHERE type='credit_card'").fetchall():
        db.execute("""INSERT OR IGNORE INTO categories (trx_type, l1, l2)
                      VALUES ('transfer', 'Credit Card Payment', ?)""",
                   (r["name"],))

    # ── Migrations: add columns that may not exist in older DBs ──────────────
    migrations = [
        "ALTER TABLE transactions ADD COLUMN reconciled_at TEXT",
        "ALTER TABLE transactions ADD COLUMN ext_ref TEXT",
        "ALTER TABLE transactions ADD COLUMN reconciled_via TEXT",
        f"ALTER TABLE transactions ADD COLUMN portal TEXT DEFAULT '{OWNER}'",
        # User-set flag that excludes a trx from the "No receipt" filter on
        # expense tables (small charges with no findable receipt).
        "ALTER TABLE transactions ADD COLUMN no_receipt_needed INTEGER DEFAULT 0",
        f"ALTER TABLE staging ADD COLUMN owner TEXT DEFAULT '{OWNER}'",
        "ALTER TABLE staging ADD COLUMN payment_date TEXT",
        "ALTER TABLE staging ADD COLUMN receipt_path TEXT",
        # Investments: every lot is stamped with the SOURCE of its money —
        # 'you' (your own capital) or 'employer' (an employer match).
        "ALTER TABLE investment_lots ADD COLUMN source TEXT DEFAULT 'you'",
    ]
    for sql in migrations:
        try:
            db.execute(sql)
        except Exception:
            pass  # column already exists

    # ── Backfill transaction_links from legacy link_id ────────────────────────
    db.execute("""
        INSERT OR IGNORE INTO transaction_links (a_id, b_id)
        SELECT MIN(id, link_id), MAX(id, link_id)
        FROM transactions
        WHERE link_id IS NOT NULL AND link_id != id AND status='active'
    """)

    # ── Receipts table (multi-receipt support + deterministic pipeline) ───────
    # Each receipt is its own row. transactions.receipt_path is kept as a
    # denormalized "primary receipt" cache for fast list-view rendering —
    # always the lowest-id receipt for the trx (auto-maintained).
    db.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            content_hash TEXT,
            file_type TEXT,
            inbox_seen_at TEXT NOT NULL DEFAULT (datetime('now')),

            extracted_vendor TEXT,
            extracted_amount REAL,
            extracted_date TEXT,
            extracted_order_id TEXT,
            extractor_used TEXT,
            extraction_confidence REAL,

            matched_trx_id INTEGER REFERENCES transactions(id),
            match_confidence TEXT,
            match_score REAL,
            candidate_trx_ids TEXT,

            status TEXT NOT NULL DEFAULT 'inbox',
            filed_path TEXT,
            duplicate_of_id INTEGER REFERENCES receipts(id),
            notes TEXT,

            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_receipts_status ON receipts(status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_receipts_trx    ON receipts(matched_trx_id)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_hash ON receipts(content_hash) WHERE content_hash IS NOT NULL")

    try:
        db.execute("ALTER TABLE receipts ADD COLUMN owner TEXT")
    except Exception:
        pass  # column already exists
    db.execute("CREATE INDEX IF NOT EXISTS idx_receipts_owner ON receipts(owner)")

    # Backfill: any receipt with a matched_trx_id inherits that trx's owner.
    db.execute("""
        UPDATE receipts
           SET owner = (SELECT owner FROM transactions
                         WHERE transactions.id = receipts.matched_trx_id)
         WHERE owner IS NULL AND matched_trx_id IS NOT NULL
    """)

    # Backfill: every transaction with receipt_path set gets a 'filed' receipts
    # row, IF one doesn't already exist. Idempotent across launches.
    backfill_rows = db.execute("""
        SELECT t.id AS trx_id, t.vendor, t.amount, t.trx_date, t.receipt_path
          FROM transactions t
         WHERE t.receipt_path IS NOT NULL
           AND t.status = 'active'
           AND NOT EXISTS (
             SELECT 1 FROM receipts r
              WHERE r.matched_trx_id = t.id
                AND r.filed_path = t.receipt_path
           )
    """).fetchall()
    for br in backfill_rows:
        path = br["receipt_path"]
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        p = path.lower()
        if   p.endswith(".pdf"):  ftype = "application/pdf"
        elif p.endswith(".png"):  ftype = "image/png"
        elif p.endswith((".jpg", ".jpeg")): ftype = "image/jpeg"
        elif p.endswith(".heic"): ftype = "image/heic"
        else: ftype = None
        db.execute("""
            INSERT INTO receipts
                (filename, file_type, inbox_seen_at,
                 extracted_vendor, extracted_amount, extracted_date,
                 extractor_used, extraction_confidence,
                 matched_trx_id, match_confidence, status, filed_path, notes)
            VALUES (?, ?, datetime('now'), ?, ?, ?, 'backfill', 1.0,
                    ?, 'HIGH', 'filed', ?,
                    'backfilled from transactions.receipt_path')
        """, (filename, ftype, br["vendor"], br["amount"], br["trx_date"],
              br["trx_id"], path))

    # ── Budget Values ─────────────────────────────────────────────────────────
    # Expense (and income) budget targets by year, at two levels:
    #   l2 = ''   → an L1-level budget row
    #   l2 = <L2> → an L2-level budget row (L2 budgets ROLL UP: when any L2
    #               under an L1 has a budget, the L1's effective budget is
    #               the sum of its L2 rows)
    # The portal column is vestigial in the single-portal build (always the
    # app OWNER) but kept for fork-ability.
    db.execute("""
        CREATE TABLE IF NOT EXISTS budget_values (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            portal      TEXT NOT NULL,
            year        INTEGER NOT NULL,
            l1          TEXT NOT NULL,
            l2          TEXT NOT NULL DEFAULT '',
            amount      REAL NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (portal, year, l1, l2)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_budget_values_portal_year ON budget_values(portal, year)")

    # ── Investment Lots + Events (EXPERIMENTAL investments module) ────────────
    # investment_lots — materialized state. One row per "lot" of principal
    # ever deployed. origin_date + origin_amount are sticky; current_account_id
    # + current_value mutate as lots earn / move / close. parent_lot_id is set
    # when a lot is created via an inter-investment move.
    #
    # investment_events — append-only ledger. kind enum: contribution |
    # withdrawal | lot_move_out | lot_move_in | snapshot | dividend |
    # interest | fee | gain_loss | closure.
    db.execute("""
        CREATE TABLE IF NOT EXISTS investment_lots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            current_account_id  INTEGER NOT NULL REFERENCES accounts(id),
            origin_date         TEXT NOT NULL,
            origin_amount       REAL NOT NULL,
            current_value       REAL NOT NULL,
            parent_lot_id       INTEGER REFERENCES investment_lots(id),
            origin_event_id     INTEGER,
            closed_at           TEXT,
            closing_event_id    INTEGER,
            note                TEXT,
            source              TEXT DEFAULT 'you',   -- 'you' | 'employer'
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_inv_lots_account ON investment_lots(current_account_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_inv_lots_parent  ON investment_lots(parent_lot_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_inv_lots_open    ON investment_lots(closed_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_inv_lots_origin  ON investment_lots(origin_date)")

    db.execute("""
        CREATE TABLE IF NOT EXISTS investment_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date      TEXT NOT NULL,
            account_id      INTEGER NOT NULL REFERENCES accounts(id),
            kind            TEXT NOT NULL,
            amount          REAL,
            lot_id          INTEGER REFERENCES investment_lots(id),
            paired_event_id INTEGER REFERENCES investment_events(id),
            linked_trx_id   INTEGER REFERENCES transactions(id),
            snapshot_value  REAL,
            note            TEXT,
            extra_json      TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_inv_events_date    ON investment_events(event_date)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_inv_events_account ON investment_events(account_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_inv_events_kind    ON investment_events(kind)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_inv_events_lot     ON investment_events(lot_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_inv_events_trx     ON investment_events(linked_trx_id)")

    # ── CC statement reconciliation (Reconcile Card tool) ────────────────────
    # Purpose-built link tables: cc_recon_payments marks a card payment as
    # reconciled (with any prior-close carry); cc_settlements ties each
    # settled charge to the payment that paid it (UNIQUE per charge, so a
    # charge can never be counted twice). statement_balances holds optional
    # per-close statement figures (keyed by the card's account_num) that
    # power the Expected lines + prior-close carry.
    db.execute("""
        CREATE TABLE IF NOT EXISTS cc_recon_payments (
            payment_id INTEGER PRIMARY KEY REFERENCES transactions(id),
            carry      REAL NOT NULL DEFAULT 0,   -- prior-close carry (e.g. opening credit)
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS cc_settlements (
            payment_id INTEGER NOT NULL REFERENCES transactions(id),
            charge_id  INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
            prev_payment_date   TEXT,             -- for Unwind
            prev_statement_date TEXT,             -- for Unwind
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (payment_id, charge_id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS statement_balances (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            coa_code       TEXT NOT NULL,     -- the card's account_num (last 4)
            statement_date TEXT NOT NULL,
            balance        REAL NOT NULL,     -- statement "New Balance" figure
            note           TEXT,
            created_at     TEXT DEFAULT (datetime('now')),
            UNIQUE(coa_code, statement_date)
        )
    """)

    # ── Payroll Reconciliation (Payroll tool) ────────────────────────────────
    # One reconciliation per paycheck (or per YTD true-up entry). Employee-
    # side numbers only: gross, taxes withheld, pre-tax deductions, net.
    # entry_mode: 'gusto' (CSV import) | 'manual' (one paystub) | 'ytd'
    # (year-to-date true-up as of a date). Single-owner build — every row
    # belongs to the app OWNER, so no owner column is needed.
    db.execute("""
        CREATE TABLE IF NOT EXISTS payroll_reconciliations (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_mode        TEXT NOT NULL DEFAULT 'gusto',

            -- Period identification
            pay_period_start  TEXT,                 -- e.g. '2026-04-01'
            pay_period_end    TEXT NOT NULL,        -- unique key (with entry_mode)
            pay_date          TEXT NOT NULL,        -- when net pay hit the bank

            -- Employee-side amounts (gross to net)
            gross_earnings    REAL NOT NULL,
            pretax_retirement REAL NOT NULL DEFAULT 0,  -- 401k / 403b etc.
            pretax_other      REAL NOT NULL DEFAULT 0,  -- health & other pre-tax
            tax_federal       REAL NOT NULL DEFAULT 0,
            tax_state         REAL NOT NULL DEFAULT 0,
            tax_ss            REAL NOT NULL DEFAULT 0,  -- or combined FICA (manual)
            tax_medicare      REAL NOT NULL DEFAULT 0,
            tax_other         REAL NOT NULL DEFAULT 0,  -- PFML / SDI / local
            posttax_roth      REAL NOT NULL DEFAULT 0,  -- Roth 401k etc. (transfer)
            posttax_garnish   REAL NOT NULL DEFAULT 0,  -- garnishment / other post-tax
            other_plug        REAL NOT NULL DEFAULT 0,  -- makes the math tie
            net_pay           REAL NOT NULL,

            -- Investments target for the pre-tax retirement transfer:
            -- an investment account NAME, 'none' = skip, NULL = auto-pick.
            retirement_account TEXT,

            -- Audit / provenance
            source_csv_filename TEXT,               -- Gusto path only
            source_csv_path     TEXT,               -- saved copy in imports/payroll/
            source_values_json  TEXT,               -- as-parsed snapshot (never overwritten)
            note                TEXT,
            status              TEXT NOT NULL DEFAULT 'draft',  -- draft | reconciled | undone
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            reconciled_at       TEXT,
            undone_at           TEXT,

            UNIQUE (pay_period_end, entry_mode)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_payroll_recon_period ON payroll_reconciliations(pay_period_end)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_payroll_recon_status ON payroll_reconciliations(status)")
    # Idempotent ALTERs for DBs created before newer columns.
    for _sql in (
        "ALTER TABLE payroll_reconciliations ADD COLUMN retirement_account TEXT",
        # Post-tax paycheck lines: Roth retirement (books as a transfer, like
        # pre-tax retirement) and garnishment/other post-tax (contra-income).
        "ALTER TABLE payroll_reconciliations ADD COLUMN posttax_roth REAL NOT NULL DEFAULT 0",
        "ALTER TABLE payroll_reconciliations ADD COLUMN posttax_garnish REAL NOT NULL DEFAULT 0",
    ):
        try:
            db.execute(_sql)
        except Exception:
            pass  # column already exists

    # Junction: reconciliation ↔ transactions it created or affected.
    # role: 'source' = bank deposit that became a split parent (pre_state_json
    # snapshots its pre-split state for undo); 'derived' = a child created by
    # the split; 'standalone' = a fresh row not tied to a parent (YTD mode /
    # no matched deposit). derived + standalone rows are DELETEd on undo.
    db.execute("""
        CREATE TABLE IF NOT EXISTS payroll_reconciliation_trxs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            reconciliation_id INTEGER NOT NULL REFERENCES payroll_reconciliations(id) ON DELETE CASCADE,
            trx_id            INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
            role              TEXT NOT NULL,
            pre_state_json    TEXT,
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (reconciliation_id, trx_id)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_payroll_recon_trxs_recon ON payroll_reconciliation_trxs(reconciliation_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_payroll_recon_trxs_trx   ON payroll_reconciliation_trxs(trx_id)")

    # ── First-run folders ─────────────────────────────────────────────────────
    for d in (config.IMPORTS_ROOT, config.IMPORTS_CHECKING_DIR,
              config.IMPORTS_CC_DIR, config.IMPORTS_OTHER_DIR,
              config.IMPORTS_PAYROLL_DIR,
              config.RECEIPTS_INBOX,
              os.path.join(config.RECEIPTS_ROOT, "filed")):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass

    db.commit()
    db.close()
