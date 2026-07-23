"""
ccrecon_test.py — end-to-end test of the DB-driven credit-card cycle:

  create a CC account WITH a statement close day (Docs & Settings →
  Accounts) → import a synthetic Chase-CC CSV (including an INTEREST
  charge) → statement_date assigned per the close day at import →
  checking-side autopay imports as a 'CC Payment' display-forked transfer
  → Reconcile Card preview ties to the penny (interest included) →
  carried-balance preview (partial payment) diagnoses instead of breaking
  → confirm trues up payment/statement dates.

Also covers the Task-1 display fork: a card-payment transfer renders
"CC Payment" and an investment transfer renders "Transfer" in the review
queue + transaction detail HTML (same stored trx_type='transfer').

Runs against a THROWAWAY DB (never your real one), same pattern as
tests/smoke.py. All data below is synthetic.

Usage (from the app folder):
    python3 tests/ccrecon_test.py
"""
import io
import os
import re
import sys
import sqlite3
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_COPY = "/tmp/pft_ccrecon_test.db"

PASS = 0


def ok(label):
    global PASS
    PASS += 1
    print(f"  ✓ {label}")


CC_CSV = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
05/27/2026,05/28/2026,STARBUCKS STORE 12345,Food & Drink,Sale,-12.50,
06/02/2026,06/02/2026,AMZN Mktp US*TEST123,Shopping,Sale,-49.99,
06/20/2026,06/20/2026,PURCHASE INTEREST CHARGE,Fees & Adjustments,Fee,-23.41,
06/24/2026,06/26/2026,TARGET 00012345,Shopping,Sale,-30.00,
06/22/2026,06/22/2026,Payment Thank You - Web,,Payment,500.00,
"""

CHECKING_CSV = """Details,Posting Date,Description,Amount,Type,Check or Slip #
DEBIT,07/22/2026,CHASE CREDIT CRD AUTOPAY  PPD ID: 4760039224,-115.90,ACH_DEBIT,
DEBIT,07/10/2026,BROKERAGE TRANSFER MANUAL DB-BKRG XXXXX9999,-1000.00,ACH_DEBIT,
"""


def main():
    if os.path.exists(DB_COPY):
        os.remove(DB_COPY)
    spec = importlib.util.spec_from_file_location("appmod",
                                                  os.path.join(ROOT, "app.py"))
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

    # ── Setup: one active CC account WITH a close day ────────────────────
    # Deactivate the seeded placeholder card so exactly ONE active credit
    # card exists (that makes the checking importer's L2 default fire).
    seeded = db.execute(
        "SELECT id FROM accounts WHERE type='credit_card'").fetchone()
    c.post("/settings/accounts",
           data={"action": "toggle", "account_id": seeded["id"]},
           follow_redirects=True)

    r = c.post("/settings/accounts", data={
        "action": "add", "name": "Test Visa", "type": "credit_card",
        "account_num": "4242", "stmt_close_day": "25", "pay_due_day": "22",
    }, follow_redirects=True)
    assert b"added" in r.data
    visa = db.execute("SELECT * FROM accounts WHERE name='Test Visa'").fetchone()
    assert visa["stmt_close_day"] == 25 and visa["pay_due_day"] == 22
    # The card-payment transfer category exists with L2 = the card's name
    assert db.execute(
        "SELECT 1 FROM categories WHERE trx_type='transfer' "
        "AND l1='Credit Card Payment' AND l2='Test Visa'").fetchone()
    ok("setup: CC account created with close day 25 / due day 22 + CCP category")

    # ── Import the card CSV (incl. the interest row) ─────────────────────
    r = c.post("/import", data={
        "account_id": str(visa["id"]),
        "csv_file": (io.BytesIO(CC_CSV.encode()), "test_visa.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200

    staged = db.execute(
        "SELECT * FROM staging WHERE account_id=? ORDER BY id",
        (visa["id"],)).fetchall()
    assert len(staged) == 5
    by_desc = {s["raw_description"]: s for s in staged}
    # Card-side 'Payment Thank You' stays skipped (never double-counts)
    assert by_desc["Payment Thank You - Web"]["status"] == "skipped"
    # Interest imports as a NORMAL pending expense — nothing special-cases it
    interest = by_desc["PURCHASE INTEREST CHARGE"]
    assert interest["status"] == "pending" and interest["trx_type"] == "expense"
    ok("import: 4 charges pending incl. interest; card-side payment row skipped")

    # ── statement_date assigned at import from the account's close day ───
    # Close day 25 → posts before the 25th land on this month's close;
    # on/after the 25th roll to next month.
    assert by_desc["STARBUCKS STORE 12345"]["statement_date"] == "2026-06-25"
    assert by_desc["AMZN Mktp US*TEST123"]["statement_date"] == "2026-06-25"
    assert interest["statement_date"] == "2026-06-25"
    # Boundary charge posted 06/26 (after the close) → next cycle
    assert by_desc["TARGET 00012345"]["statement_date"] == "2026-07-25"
    ok("import: statement_date assigned per close day (boundary rolls forward)")

    # ── Approve every pending charge ─────────────────────────────────────
    for s in staged:
        if s["status"] == "pending":
            r = c.post(f"/api/review/{s['id']}/approve", json={})
            assert r.status_code == 200, r.data
    trx = db.execute(
        "SELECT * FROM transactions WHERE account_id=? ORDER BY id",
        (visa["id"],)).fetchall()
    assert len(trx) == 4
    t_by_desc = {t["raw_description"]: t for t in trx}
    ti = t_by_desc["PURCHASE INTEREST CHARGE"]
    # Interest landed as a plain expense (+23.41 after the approve sign flip)
    assert ti["trx_type"] == "expense" and abs(ti["amount"] - 23.41) < 0.005
    assert ti["statement_date"] == "2026-06-25"
    ok("approve: charges committed; interest is a normal expense in the 06-25 cycle")

    # ── Checking side: autopay imports as a CC Payment transfer ──────────
    checking = db.execute(
        "SELECT * FROM accounts WHERE type='checking' AND active=1").fetchone()
    r = c.post("/import", data={
        "account_id": str(checking["id"]),
        "csv_file": (io.BytesIO(CHECKING_CSV.encode()), "test_checking.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200
    pay_s = db.execute(
        "SELECT * FROM staging WHERE account_id=? AND raw_description LIKE "
        "'%AUTOPAY%'", (checking["id"],)).fetchone()
    inv_s = db.execute(
        "SELECT * FROM staging WHERE account_id=? AND raw_description LIKE "
        "'%DB-BKRG%'", (checking["id"],)).fetchone()
    assert pay_s["status"] == "pending" and pay_s["trx_type"] == "transfer"
    assert pay_s["l1_category"] == "Credit Card Payment"
    assert pay_s["l2_category"] == "Test Visa"   # single-card default
    assert inv_s["trx_type"] == "transfer" and inv_s["l1_category"] is None
    ok("checking import: autopay kept as CCP transfer (L2 = card); "
       "investment row a plain transfer")

    # ── Display fork in the review queue HTML ────────────────────────────
    html = c.get("/review").data.decode()

    def _type_select(staging_id):
        m = re.search(
            r'id="d-type-%d".*?</select>' % staging_id, html, re.S)
        assert m, f"type select for staging {staging_id} not found"
        return m.group(0)

    sel_pay = _type_select(pay_s["id"])
    assert re.search(r'value="cc_payment"\s+selected', sel_pay)
    assert not re.search(r'value="transfer"\s+selected', sel_pay)
    sel_inv = _type_select(inv_s["id"])
    assert re.search(r'value="transfer"\s+selected', sel_inv)
    assert not re.search(r'value="cc_payment"\s+selected', sel_inv)
    ok("display fork (review): card payment shows 'CC Payment', "
       "investment transfer shows 'Transfer'")

    # ── Approve both checking rows ───────────────────────────────────────
    r = c.post(f"/api/review/{pay_s['id']}/approve", json={})
    assert r.status_code == 200
    r = c.post(f"/api/review/{inv_s['id']}/approve", json={
        "l1_category": "Retirement", "l2_category": "Retirement Plan"})
    assert r.status_code == 200
    pay_t = db.execute(
        "SELECT * FROM transactions WHERE l1_category='Credit Card Payment' "
        "AND trx_type='transfer'").fetchone()
    inv_t = db.execute(
        "SELECT * FROM transactions WHERE l1_category='Retirement' "
        "AND trx_type='transfer'").fetchone()
    assert pay_t and inv_t

    # ── Display fork on the transaction detail page ──────────────────────
    dhtml = c.get(f"/transactions/{pay_t['id']}").data.decode()
    m = re.search(r'id="f-trx_type".*?</select>', dhtml, re.S)
    assert m and re.search(r'value="cc_payment"\s+selected', m.group(0))
    dhtml = c.get(f"/transactions/{inv_t['id']}").data.decode()
    m = re.search(r'id="f-trx_type".*?</select>', dhtml, re.S)
    assert m and re.search(r'value="transfer"\s+selected', m.group(0))
    assert not re.search(r'value="cc_payment"\s+selected', m.group(0))
    ok("display fork (trx detail): CC Payment vs Transfer over the same "
       "stored type")

    # ── Wizard preview: boundary charge diagnosed, then stmt-date pulled in ──
    # Payment 115.90 = 12.50 + 49.99 + 23.41 (interest) + 30.00 (boundary).
    # The boundary charge's importer-assigned statement date (07-25) keeps
    # it OUT of the 06-25 window at first → residual +30 with a one-click
    # include suggestion.
    r = c.post("/api/ccrecon/preview", json={"payment_id": pay_t["id"]})
    d = r.get_json()
    assert not d.get("error"), d
    assert d["cycle"] == "2026-06-25" and not d["ok"]
    assert abs(d["residual"] - 30.00) < 0.005
    target_t = t_by_desc["TARGET 00012345"]
    assert any(s["id"] == target_t["id"] and s["action"] == "include"
               for s in d["diagnosis"]["singles"])
    ok("wizard: boundary charge out of window → +30.00 residual with "
       "include suggestion")

    # Statement-date-first bucketing: correcting the boundary charge's
    # statement_date to the 06-25 close pulls it into the window even
    # though its POST date (06-26) is after the close.
    r = c.post(f"/api/transactions/{target_t['id']}",
               json={"statement_date": "2026-06-25"})
    assert r.status_code == 200
    r = c.post("/api/ccrecon/preview", json={"payment_id": pay_t["id"]})
    d = r.get_json()
    assert d["ok"] and abs(d["residual"]) < 0.005
    ids = {ch["id"] for ch in d["charges"]}
    assert target_t["id"] in ids and ti["id"] in ids
    assert len(d["charges"]) == 4
    ok("wizard: statement-date basis buckets the charge into 06-25; "
       "tie includes the interest charge (residual $0.00)")

    # ── Carried balance: a partial payment diagnoses, never breaks ───────
    import hashlib
    dk = hashlib.md5(b"2026-07-22|PARTIAL PAY|60.00").hexdigest()
    db.execute("""
        INSERT INTO transactions
            (account_id, trx_date, raw_description, vendor, amount, trx_type,
             owner, l1_category, l2_category, dedup_key, status)
        VALUES (?, '2026-07-22', 'PARTIAL PAY', 'Test Visa', -60.00,
                'transfer', 'ME', 'Credit Card Payment', 'Test Visa', ?, 'active')
    """, (checking["id"], dk))
    db.commit()
    partial_id = db.execute(
        "SELECT id FROM transactions WHERE raw_description='PARTIAL PAY'"
    ).fetchone()["id"]
    r = c.post("/api/ccrecon/preview", json={"payment_id": partial_id})
    d = r.get_json()
    assert not d.get("error") and not d["ok"]
    cb = d["diagnosis"]["carried_balance"]
    assert cb and abs(cb["amount"] - 55.90) < 0.005, d["diagnosis"]
    ok("wizard: partial payment → carried-balance diagnosis "
       "(unpaid 55.90 shown, not an error)")

    # ── Confirm the full payment: dates true up, tie verified ────────────
    r = c.post("/api/ccrecon/confirm", json={"payment_id": pay_t["id"]})
    d = r.get_json()
    assert d.get("applied") and d["ties"] and d["n_charges"] == 4, d
    for t in db.execute(
            "SELECT * FROM transactions WHERE account_id=?", (visa["id"],)):
        assert t["payment_date"] == "2026-07-22"
        assert t["statement_date"] == "2026-06-25"
    ok("wizard: confirm settles all 4 charges; payment/statement dates trued")

    print(f"\nCCRECON TESTS PASSED — {PASS} checks green")


if __name__ == "__main__":
    main()
