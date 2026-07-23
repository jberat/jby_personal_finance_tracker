"""
smoke.py — functional smoke suite for Personal Financial Tracker.

Runs against a THROWAWAY DB (never your real one). Exercises the import →
review → approve pipeline, accounting invariants, and page renders.
Run after any change; must exit 0.

Usage (from the app folder):
    python3 tests/smoke.py
"""
import sys
import os
import io
import sqlite3
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_COPY = "/tmp/pft_smoke.db"

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

    # ── Auth ──────────────────────────────────────────────────────────────
    r = c.post("/login", data={"password": "definitely-wrong"})
    assert b"Wrong password" in r.data
    r = c.post("/api/transactions/1/delete",
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok("auth: wrong password rejected; cross-origin POST blocked")

    with c.session_transaction() as s:
        s["logged_in"] = True

    r = c.get("/api/receipts/preview?path=/etc/passwd")
    assert r.status_code == 403
    ok("security: receipt preview path traversal blocked")

    db = sqlite3.connect(DB_COPY)
    db.row_factory = sqlite3.Row
    acct = {row["type"]: row["id"] for row in db.execute("SELECT id, type FROM accounts")}

    # ── Import → review → approve round trip ─────────────────────────────
    cc_csv = ("Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
              "01/05/2026,01/06/2026,TRADER JOE S #123,Groceries,Sale,-84.52,\n"
              "01/07/2026,01/08/2026,AUTOMATIC PAYMENT - THANK,,Payment,500.00,\n")
    r = c.post("/import", data={"account_id": acct["credit_card"],
        "csv_file": (io.BytesIO(cc_csv.encode()), "t.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200
    row = db.execute("SELECT * FROM staging WHERE raw_description LIKE 'TRADER%'").fetchone()
    assert row and row["status"] == "pending" and row["owner"] == "ME"
    skip = db.execute("SELECT status FROM staging WHERE raw_description LIKE 'AUTOMATIC%'").fetchone()
    assert skip["status"] == "skipped"
    ok("import: CSV staged, CC payment auto-skipped, owner stamped")

    d = c.post(f"/api/review/{row['id']}/approve",
               json={"l1_category": "Food & Dining",
                     "l2_category": "Groceries"}).get_json()
    assert d.get("ok")
    trx = db.execute("SELECT * FROM transactions WHERE staging_id=?",
                     (row["id"],)).fetchone()
    assert trx["trx_type"] == "expense" and abs(trx["amount"] - 84.52) < 0.01
    ok("approve: sign flipped on expense commit")

    # ── Venmo importer ────────────────────────────────────────────────────
    venmo_csv = (
        "Account Statement\nAccount Activity\n"
        ",ID,Datetime,Type,Status,Note,From,To,Amount (total)\n"
        ",41001,2026-01-04T18:22:01,Payment,Complete,Dinner,Me,Alex Friend,- $42.50\n"
        ",41002,2026-01-06T09:10:00,Standard Transfer,Issued,,Me,,- $500.00\n")
    r = c.post("/import", data={"account_id": acct["digital_wallet"],
        "csv_file": (io.BytesIO(venmo_csv.encode()), "v.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200
    v = db.execute("SELECT * FROM staging WHERE note='Dinner'").fetchone()
    assert v and v["trx_type"] == "expense" and v["vendor"] == "Alex Friend"
    ok("venmo: statement parsed, counterparty extracted")

    # ── Split invariant ──────────────────────────────────────────────────
    d = c.post(f"/api/transactions/{trx['id']}/split",
               json={"splits": [
                   {"amount": 50.00, "l1_category": "Food & Dining", "l2_category": "Groceries"},
                   {"amount": 34.52, "l1_category": "Shopping", "l2_category": "Home Goods"},
               ]}).get_json()
    assert d.get("ok"), d
    kids = db.execute("SELECT * FROM transactions WHERE parent_id=? AND status='active'",
                      (trx["id"],)).fetchall()
    assert abs(sum(k["amount"] for k in kids) - 84.52) < 0.01
    ok("split: family sums to parent")

    # ── L2 budget rollup ─────────────────────────────────────────────────
    for body in ({"year": 2026, "l1": "Travel", "amount": 5000},
                 {"year": 2026, "l1": "Travel", "l2": "Airfare", "amount": 2000},
                 {"year": 2026, "l1": "Travel", "l2": "Lodging", "amount": 1500}):
        assert c.post("/api/budget-values/upsert", json=body).get_json()["ok"]
    html = c.get("/tools/actuals-vs-budget?year=2026").data.decode()
    assert "3,500" in html and "5,000" not in html
    ok("budgets: L2 rollup (2000+1500) overrides flat L1 (5000)")

    # ── Pages render ──────────────────────────────────────────────────────
    for url in ["/", "/review", "/expenses/overview", "/income/overview",
                "/investments/overview", "/receipts/review", "/tools/cleanup",
                "/settings/assumptions/budget-values", "/trash"]:
        assert c.get(url, follow_redirects=True).status_code == 200, url
    ok("pages: core routes render")

    # ── Exports ───────────────────────────────────────────────────────────
    r = c.get("/export/expenses-transactions.xlsx?date_from=2026-01-01&date_to=2026-12-31")
    assert r.status_code == 200
    ok("exports: xlsx endpoint returns a workbook")

    print(f"\nSMOKE PASSED — {PASS} checks green")


if __name__ == "__main__":
    main()
