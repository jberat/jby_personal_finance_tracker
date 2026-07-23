"""
accounts_test.py — functional tests for the Accounts manager
(Docs & Settings → Accounts) and the zero-state overview tables.

Runs against a THROWAWAY DB (never your real one), same pattern as
tests/smoke.py. All data below is synthetic.

Usage (from the app folder):
    python3 tests/accounts_test.py
"""
import sys
import os
import hashlib
import sqlite3
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_COPY = "/tmp/pft_accounts_test.db"

PASS = 0


def ok(label):
    global PASS
    PASS += 1
    print(f"  ✓ {label}")


def main():
    if os.path.exists(DB_COPY):
        os.remove(DB_COPY)
    spec = importlib.util.spec_from_file_location("appmod", os.path.join(ROOT, "app.py"))
    appmod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, ROOT)
    spec.loader.exec_module(appmod)
    appmod.config.DB_PATH = DB_COPY
    appmod.init_db()
    app = appmod.app
    app.config["TESTING"] = True
    c = app.test_client()

    with c.session_transaction() as s:
        s["logged_in"] = True

    db = sqlite3.connect(DB_COPY)
    db.row_factory = sqlite3.Row

    # ── Zero-state overviews on a completely EMPTY fresh DB ──────────────
    hint = b"Showing your full category tree"
    r = c.get("/expenses/overview")
    assert r.status_code == 200
    assert b"Personal Care" in r.data and b"Housing" in r.data, \
        "empty DB: expenses overview should render the FULL canonical tree"
    assert hint in r.data
    ok("zero-state: /expenses/overview renders full category tree at $0 + hint")

    r = c.get("/income/overview")
    assert r.status_code == 200
    assert b"Salary &amp; Wages" in r.data and b"Interest &amp; Dividends" in r.data
    assert hint in r.data
    ok("zero-state: /income/overview renders full category tree at $0 + hint")

    # ── Import page renders with the dropzone markup ─────────────────────
    r = c.get("/import")
    assert r.status_code == 200
    assert b"import-dropzone" in r.data and b"csv-file-input" in r.data
    assert b"dz-filename" in r.data
    ok("import page: dropzone markup present")

    # ── Accounts page renders ────────────────────────────────────────────
    r = c.get("/settings/accounts")
    assert r.status_code == 200
    assert b"Add an Account" in r.data and b"My Checking" in r.data
    ok("accounts page: renders with seeded accounts")

    # ── Add account via POST → appears everywhere it should ──────────────
    r = c.post("/settings/accounts", data={
        "action": "add", "name": "Test Rewards Card",
        "type": "credit_card", "account_num": "9876",
        "stmt_close_day": "15",   # REQUIRED for credit-card adds
    }, follow_redirects=True)
    assert r.status_code == 200 and b"added" in r.data
    row = db.execute("SELECT * FROM accounts WHERE name='Test Rewards Card'").fetchone()
    assert row and row["owner"] == "ME" and row["active"] == 1 \
        and row["type"] == "credit_card" and row["account_num"] == "9876"
    assert row["stmt_close_day"] == 15
    ok("add: POST inserts row with owner='ME', active=1")

    # Appears in the import dropdown source query (the exact filter
    # import_csv uses) AND in the rendered import page.
    importable = [a["name"] for a in db.execute(
        "SELECT name FROM accounts WHERE owner='ME' AND active=1 "
        "AND type IN ('credit_card','checking','digital_wallet')")]
    assert "Test Rewards Card" in importable
    assert b"Test Rewards Card" in c.get("/import").data
    ok("add: new account appears in import dropdown source query + page")

    # ── Duplicate name rejected (case-insensitive); empty name rejected ──
    r = c.post("/settings/accounts", data={
        "action": "add", "name": "test rewards CARD", "type": "checking",
    }, follow_redirects=True)
    assert b"already exists" in r.data
    n = db.execute("SELECT COUNT(*) FROM accounts "
                   "WHERE LOWER(name)=LOWER('Test Rewards Card')").fetchone()[0]
    assert n == 1
    r = c.post("/settings/accounts", data={
        "action": "add", "name": "   ", "type": "checking",
    }, follow_redirects=True)
    assert b"empty" in r.data
    ok("guards: duplicate name (case-insensitive) and empty name rejected")

    # ── Credit-card adds REQUIRE a statement close day ───────────────────
    r = c.post("/settings/accounts", data={
        "action": "add", "name": "No Close Day Card", "type": "credit_card",
    }, follow_redirects=True)
    assert b"statement close day" in r.data, \
        "credit-card add without a close day must be rejected with a hint"
    assert db.execute("SELECT COUNT(*) FROM accounts "
                      "WHERE name='No Close Day Card'").fetchone()[0] == 0
    # Out-of-range and non-numeric close days rejected too
    r = c.post("/settings/accounts", data={
        "action": "add", "name": "No Close Day Card",
        "type": "credit_card", "stmt_close_day": "40",
    }, follow_redirects=True)
    assert b"between 1 and 31" in r.data
    r = c.post("/settings/accounts", data={
        "action": "add", "name": "No Close Day Card",
        "type": "credit_card", "stmt_close_day": "soon",
    }, follow_redirects=True)
    assert b"whole number" in r.data
    assert db.execute("SELECT COUNT(*) FROM accounts "
                      "WHERE name='No Close Day Card'").fetchone()[0] == 0
    ok("add: credit card without a valid close day (missing/40/'soon') rejected")

    # ── Other account types are unaffected by the close-day rule ─────────
    r = c.post("/settings/accounts", data={
        "action": "add", "name": "Plain Checking", "type": "checking",
    }, follow_redirects=True)
    assert b"added" in r.data
    pc = db.execute("SELECT * FROM accounts WHERE name='Plain Checking'").fetchone()
    assert pc and pc["stmt_close_day"] is None and pc["pay_due_day"] is None
    ok("add: non-CC types add fine with no billing days (rule is CC-only)")

    # ── Edit flow: an existing card gains its billing days ───────────────
    # (This is how cards created before the feature — or added without a
    # due day — get their days filled in.)
    r = c.post("/settings/accounts", data={
        "action": "rename", "account_id": row["id"],
        "name": "Test Rewards Card",          # unchanged
        "stmt_close_day": "25", "pay_due_day": "22",
    }, follow_redirects=True)
    assert b"statement close day" in r.data and b"payment due day" in r.data
    upd = db.execute("SELECT stmt_close_day, pay_due_day FROM accounts "
                     "WHERE id=?", (row["id"],)).fetchone()
    assert upd["stmt_close_day"] == 25 and upd["pay_due_day"] == 22
    # A CC add also seeds its card-payment transfer category (L2 = card name)
    assert db.execute(
        "SELECT 1 FROM categories WHERE trx_type='transfer' "
        "AND l1='Credit Card Payment' AND l2='Test Rewards Card'").fetchone()
    ok("edit: existing card gains close/due days via the row form; "
       "CCP transfer category seeded")

    # ── Rename persists ──────────────────────────────────────────────────
    r = c.post("/settings/accounts", data={
        "action": "rename", "account_id": row["id"], "name": "Renamed Card",
    }, follow_redirects=True)
    assert b"Renamed" in r.data
    assert db.execute("SELECT name FROM accounts WHERE id=?",
                      (row["id"],)).fetchone()["name"] == "Renamed Card"
    # Rename to an existing name (case-insensitive) is rejected
    r = c.post("/settings/accounts", data={
        "action": "rename", "account_id": row["id"], "name": "my checking",
    }, follow_redirects=True)
    assert b"already exists" in r.data
    ok("rename: persists; rename onto an existing name rejected")

    # ── Give the account one transaction, then deactivate ────────────────
    dk = hashlib.md5(b"2026-03-05|TEST VENDOR|12.34").hexdigest()
    db.execute("""
        INSERT INTO transactions
            (account_id, trx_date, raw_description, vendor, amount,
             trx_type, owner, l1_category, l2_category, dedup_key, status)
        VALUES (?, '2026-03-05', 'TEST VENDOR', 'Test Vendor', 12.34,
                'expense', 'ME', 'Housing', 'Rent & Mortgage', ?, 'active')
    """, (row["id"], dk))
    db.commit()

    r = c.post("/settings/accounts", data={
        "action": "toggle", "account_id": row["id"],
    }, follow_redirects=True)
    assert b"deactivated" in r.data
    assert db.execute("SELECT active FROM accounts WHERE id=?",
                      (row["id"],)).fetchone()["active"] == 0
    # Excluded from the active-account (import dropdown) query...
    importable = [a["name"] for a in db.execute(
        "SELECT name FROM accounts WHERE owner='ME' AND active=1 "
        "AND type IN ('credit_card','checking','digital_wallet')")]
    assert "Renamed Card" not in importable
    assert b"Renamed Card" not in c.get("/import").data
    ok("deactivate: excluded from active-account query + import dropdown")

    # ...but its transactions still JOIN and render (history stays valid).
    joined = db.execute("""
        SELECT t.id, a.name FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.vendor='Test Vendor' AND t.status='active'
    """).fetchone()
    assert joined and joined["name"] == "Renamed Card"
    html = c.get("/expenses/transactions?year=2026").data
    assert b"Test Vendor" in html and b"Renamed Card" in html
    ok("deactivate: transactions on the account still join + render")

    # ── Zero-state reverted now that data exists ─────────────────────────
    r = c.get("/expenses/overview?year=2026")
    assert r.status_code == 200
    assert b"Housing" in r.data                    # the used L1 shows
    assert b"Personal Care" not in r.data, \
        "with data present, unused $0 L1s must be auto-hidden again"
    assert hint not in r.data
    ok("zero-state: reverts to auto-hide once any transaction exists")

    # Reactivate round-trips
    r = c.post("/settings/accounts", data={
        "action": "toggle", "account_id": row["id"],
    }, follow_redirects=True)
    assert b"reactivated" in r.data
    assert db.execute("SELECT active FROM accounts WHERE id=?",
                      (row["id"],)).fetchone()["active"] == 1
    ok("reactivate: toggle round-trips back to active")

    print(f"\nACCOUNTS TEST PASSED — {PASS} checks green")


if __name__ == "__main__":
    main()
