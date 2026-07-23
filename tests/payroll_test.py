"""
payroll_test.py — functional tests for the Payroll tool (routes_payroll).

Runs against a THROWAWAY DB (never your real one), same pattern as
tests/smoke.py. All data below is synthetic — invented names and amounts.

Usage (from the app folder):
    python3 tests/payroll_test.py
"""
import sys
import os
import io
import sqlite3
import hashlib
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_COPY = "/tmp/pft_payroll_test.db"

PASS = 0


def ok(label):
    global PASS
    PASS += 1
    print(f"  ✓ {label}")


# ── Synthetic Gusto Payroll Journal Report CSV (2 pay periods) ────────────
# Column names match what parse_gusto_csv classifies: exact "Gross Earnings"
# / "Net Pay", "... (Employee)" tax columns, "... (Employee Deduction)"
# pre-tax columns; employer-side columns must be ignored.
GUSTO_HDR = ("Last Name,First Name,Gross Earnings,"
             "Traditional 401(k) (Employee Deduction),"
             "Federal Income Tax (Employee),Social Security (Employee),"
             "Medicare (Employee),CA Withholding Tax (Employee),"
             "CA Paid Family Leave (Employee),Net Pay,"
             "Social Security (Employer),Medicare (Employer),FUTA (Employer),"
             "Employer Cost")

# P1: 4000 − 200 − 480 − 248 − 58 − 160 − 4 = 2850.00
# P2: 4100 − 205 − 495 − 254.20 − 59.45 − 165 − 4.10 = 2917.25
GUSTO_CSV = f"""Payroll period, 01/01/2026 - 01/15/2026
Pay day, 01/20/2026
{GUSTO_HDR}
Doe,Jane,4000.00,200.00,480.00,248.00,58.00,160.00,4.00,2850.00,248.00,58.00,42.00,4348.00
Payroll Totals,,4000.00,200.00,480.00,248.00,58.00,160.00,4.00,2850.00,248.00,58.00,42.00,4348.00
Payroll period, 01/16/2026 - 01/31/2026
Pay day, 02/05/2026
{GUSTO_HDR}
Doe,Jane,4100.00,205.00,495.00,254.20,59.45,165.00,4.10,2917.25,254.20,59.45,42.00,4455.65
Payroll Totals,,4100.00,205.00,495.00,254.20,59.45,165.00,4.10,2917.25,254.20,59.45,42.00,4455.65
"""


def insert_deposit(db, account_id, trx_date, amount, desc):
    """Insert a fake approved bank deposit (income trx), as an import would."""
    dk = hashlib.md5(f"{trx_date}|{desc}|{amount:.2f}".encode()).hexdigest()
    cur = db.execute("""
        INSERT INTO transactions
          (account_id, trx_date, post_date, raw_description, vendor, amount,
           trx_type, owner, l1_category, l2_category, status, dedup_key)
        VALUES (?,?,?,?,?,?, 'income','ME','Salary & Wages','Primary Job',
                'active', ?)
    """, (account_id, trx_date, trx_date, desc, "Acme Corp Payroll",
          amount, dk))
    db.commit()
    return cur.lastrowid


def main():
    if os.path.exists(DB_COPY):
        os.remove(DB_COPY)
    spec = importlib.util.spec_from_file_location("appmod",
                                                  os.path.join(ROOT, "app.py"))
    appmod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, ROOT)
    spec.loader.exec_module(appmod)
    appmod.config.DB_PATH = DB_COPY
    appmod.config.IMPORTS_PAYROLL_DIR = "/tmp/pft_payroll_test_imports"
    appmod.init_db()
    app = appmod.app
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True

    import routes_payroll as rp

    db = sqlite3.connect(DB_COPY)
    db.row_factory = sqlite3.Row
    checking_id = db.execute(
        "SELECT id FROM accounts WHERE type='checking' LIMIT 1").fetchone()["id"]

    # ── 1) Gusto CSV parse: 2 synthetic periods ──────────────────────────
    periods = rp.parse_gusto_csv(GUSTO_CSV)
    assert len(periods) == 2, f"expected 2 periods, got {len(periods)}"
    p1, p2 = periods
    assert p1["pay_period_start"] == "2026-01-01"
    assert p1["pay_period_end"] == "2026-01-15"
    assert p1["pay_date"] == "2026-01-20"
    assert p1["employee_name"] == "Jane Doe"
    assert abs(p1["gross_earnings"] - 4000.00) < 0.005
    assert abs(p1["pretax_retirement"] - 200.00) < 0.005
    assert abs(p1["tax_federal"] - 480.00) < 0.005
    assert abs(p1["tax_ss"] - 248.00) < 0.005
    assert abs(p1["tax_medicare"] - 58.00) < 0.005
    assert abs(p1["tax_state"] - 160.00) < 0.005     # CA Withholding Tax
    assert abs(p1["tax_other"] - 4.00) < 0.005       # CA Paid Family Leave
    assert abs(p1["net_pay"] - 2850.00) < 0.005
    assert p1["net_pay_matches"], "employee-side math must tie (employer cols ignored)"
    assert abs(p2["net_pay"] - 2917.25) < 0.005 and p2["net_pay_matches"]
    ok("gusto parse: 2 periods, employee-side fields bucketed, math ties")

    # ── 2) Net-deposit match: fake approved bank deposit is found ────────
    dep_id = insert_deposit(db, checking_id, "2026-01-21", 2850.00,
                            "ACH CREDIT ACME CORP PAYROLL")
    r = c.post("/tools/payroll/draft", data={
        "entry_mode": "gusto",
        "pay_period_start": p1["pay_period_start"],
        "pay_period_end": p1["pay_period_end"],
        "pay_date": p1["pay_date"],
        "gross_earnings": "4000.00", "pretax_retirement": "200.00",
        "tax_federal": "480.00", "tax_state": "160.00",
        "tax_ss": "248.00", "tax_medicare": "58.00", "tax_other": "4.00",
        "net_pay": "2850.00",
        "source_csv_filename": "test-payroll-journal.csv",
    }, follow_redirects=True)
    assert r.status_code == 200
    rec = db.execute("""SELECT * FROM payroll_reconciliations
                         WHERE pay_period_end='2026-01-15'""").fetchone()
    assert rec and rec["status"] == "draft"
    m = rp.find_payroll_matches(db, rec)["net_deposit"]
    assert m["status"] == "matched" and m["trx"]["id"] == dep_id, m["status"]
    ok("match: imported net deposit found by amount + date proximity")

    # ── 3) Full gusto apply: deposit split into gross-up rows that tie ───
    d = c.post(f"/tools/payroll/{rec['id']}/execute").get_json()
    assert d.get("ok"), d
    assert d["mode"] == "split"
    parent = db.execute("SELECT * FROM transactions WHERE id=?",
                        (dep_id,)).fetchone()
    assert parent["is_split"] == 1 and parent["status"] == "deleted"
    kids = db.execute("""SELECT * FROM transactions
                          WHERE parent_id=? AND status='active'""",
                      (dep_id,)).fetchall()
    assert kids, "split children must exist"
    assert abs(sum(k["amount"] for k in kids) - 2850.00) < 0.01, \
        "children must sum to net"
    gross_rows = [k for k in kids if k["l1_category"] == "Salary & Wages"]
    assert len(gross_rows) == 1 and abs(gross_rows[0]["amount"] - 4000.00) < 0.005, \
        "booked income must equal gross"
    tax_sum = sum(k["amount"] for k in kids if k["l1_category"] == "Taxes")
    assert abs(tax_sum - (-(480.00 + 248.00 + 58.00 + 160.00 + 4.00))) < 0.01, \
        "taxes rows must equal withholdings (contra-income)"
    ss_rows = [k for k in kids if k["l2_category"] == "Social Security"]
    med_rows = [k for k in kids if k["l2_category"] == "Medicare"]
    assert ss_rows and med_rows, "gusto path books SS and Medicare separately"
    # Retirement is a TRANSFER, not contra-income. No investments account
    # exists yet → generic fallback category, and no event syncs.
    ret_rows = [k for k in kids if k["trx_type"] == "transfer"]
    assert len(ret_rows) == 1 and abs(ret_rows[0]["amount"] - (-200.00)) < 0.005
    assert ret_rows[0]["l1_category"] == "Retirement"
    assert ret_rows[0]["l2_category"] == "Retirement Plan"
    n_ev = db.execute("SELECT COUNT(*) n FROM investment_events").fetchone()["n"]
    assert n_ev == 0, "no investments account -> no event"
    ok("gusto apply: deposit split; income=gross, taxes=withheld, "
       "retirement=transfer (fallback cat), sum=net")

    # ── 4) Undo: children deleted, deposit restored ──────────────────────
    d = c.post(f"/tools/payroll/{rec['id']}/undo").get_json()
    assert d.get("ok"), d
    parent = db.execute("SELECT * FROM transactions WHERE id=?",
                        (dep_id,)).fetchone()
    assert parent["status"] == "active" and parent["is_split"] == 0
    left = db.execute("""SELECT COUNT(*) n FROM transactions
                          WHERE parent_id=?""", (dep_id,)).fetchone()["n"]
    assert left == 0, "derived rows must be deleted on undo"
    ok("undo: booked rows deleted, original deposit restored")

    # ── 5) Manual single-paycheck apply (no imported deposit → standalone)
    # With an Investments account set up first: the retirement transfer must
    # target it and sync a contribution event via the normal Sync engine.
    d = c.post("/api/investments/account/add",
               json={"name": "My 401k", "group": "Retirement",
                     "provider": "Example Broker"}).get_json()
    assert d.get("ok"), d
    inv_acct_id = d["id"]
    # 5000 − 600 − 250 − 382.50 − 300 − 150 = 3317.50
    r = c.post("/tools/payroll/draft", data={
        "entry_mode": "manual", "pay_date": "2026-03-05",
        "gross_earnings": "5000.00", "tax_federal": "600.00",
        "tax_state": "250.00", "tax_ss": "382.50",
        "pretax_retirement": "300.00", "pretax_other": "150.00",
        "net_pay": "3317.50",
    }, follow_redirects=True)
    assert r.status_code == 200
    mrec = db.execute("""SELECT * FROM payroll_reconciliations
                          WHERE entry_mode='manual' AND pay_date='2026-03-05'
                       """).fetchone()
    assert mrec, "manual draft must save"
    plan = rp.plan_payroll_splits(mrec)
    assert plan["ties"], plan
    d = c.post(f"/tools/payroll/{mrec['id']}/execute").get_json()
    assert d.get("ok"), d
    assert d["mode"] == "standalone"
    rows = db.execute("""
        SELECT t.* FROM transactions t
          JOIN payroll_reconciliation_trxs a ON a.trx_id = t.id
         WHERE a.reconciliation_id=? AND a.role='standalone'
    """, (mrec["id"],)).fetchall()
    assert rows and abs(sum(t["amount"] for t in rows) - 3317.50) < 0.01, \
        "standalone rows must sum to net"
    fica = [t for t in rows if t["l2_category"] == "FICA (SS + Medicare)"]
    assert len(fica) == 1 and abs(fica[0]["amount"] - (-382.50)) < 0.005, \
        "manual path books one combined FICA row"
    mret = [t for t in rows if t["trx_type"] == "transfer"]
    assert len(mret) == 1 and abs(mret[0]["amount"] - (-300.00)) < 0.005
    assert mret[0]["l1_category"] == "Retirement" \
        and mret[0]["l2_category"] == "My 401k", \
        "retirement transfer must auto-target the investments account"
    ok("manual apply: gross−taxes−pretax=net; standalone rows booked + tie")

    # ── 5b) Investments wiring: contribution event synced, undo removes it ─
    ev = db.execute("""SELECT * FROM investment_events
                        WHERE linked_trx_id=?""", (mret[0]["id"],)).fetchone()
    assert ev, "sync must create a contribution event for the transfer"
    assert ev["kind"] == "contribution"
    assert abs(ev["amount"] - 300.00) < 0.005
    assert ev["event_date"] == "2026-03-05", ev["event_date"]
    assert ev["account_id"] == inv_acct_id
    lot = db.execute("SELECT * FROM investment_lots WHERE id=?",
                     (ev["lot_id"],)).fetchone()
    assert lot and abs(lot["origin_amount"] - 300.00) < 0.005
    ok("investments: sync created $300 contribution event on pay date")

    d = c.post(f"/tools/payroll/{mrec['id']}/undo").get_json()
    assert d.get("ok"), d
    assert db.execute("SELECT COUNT(*) n FROM investment_events "
                      "WHERE account_id=?", (inv_acct_id,)).fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) n FROM investment_lots "
                      "WHERE current_account_id=?", (inv_acct_id,)).fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) n FROM transactions t "
                      "JOIN payroll_reconciliation_trxs a ON a.trx_id=t.id "
                      "WHERE a.reconciliation_id=?", (mrec["id"],)).fetchone()["n"] == 0
    ok("investments: undo removed the synced event + lot and the booked rows")

    # ── 6) YTD-mode apply ────────────────────────────────────────────────
    # 30000 − 3600 − 1500 − 2295 − 1800 − 900 = 19905.00
    r = c.post("/tools/payroll/draft", data={
        "entry_mode": "ytd", "pay_date": "2026-06-30",
        "pay_period_end": "2026-06-30",
        "gross_earnings": "30000.00", "tax_federal": "3600.00",
        "tax_state": "1500.00", "tax_ss": "2295.00",
        "pretax_retirement": "1800.00", "pretax_other": "900.00",
        "net_pay": "19905.00",
    }, follow_redirects=True)
    assert r.status_code == 200
    yrec = db.execute("""SELECT * FROM payroll_reconciliations
                          WHERE entry_mode='ytd'""").fetchone()
    assert yrec and yrec["pay_period_start"] == "2026-01-01", \
        "YTD period defaults to Jan 1 of the as-of year"
    assert rp.find_payroll_matches(db, yrec)["net_deposit"]["status"] == "not_applicable"
    d = c.post(f"/tools/payroll/{yrec['id']}/execute").get_json()
    assert d.get("ok"), d
    assert d["mode"] == "standalone"
    yrows = db.execute("""
        SELECT t.* FROM transactions t
          JOIN payroll_reconciliation_trxs a ON a.trx_id = t.id
         WHERE a.reconciliation_id=?
    """, (yrec["id"],)).fetchall()
    assert abs(sum(t["amount"] for t in yrows) - 19905.00) < 0.01
    ygross = [t for t in yrows if t["l1_category"] == "Salary & Wages"]
    assert len(ygross) == 1 and abs(ygross[0]["amount"] - 30000.00) < 0.005
    yret = [t for t in yrows if t["trx_type"] == "transfer"]
    assert len(yret) == 1 and yret[0]["l2_category"] == "My 401k"
    yev = db.execute("SELECT * FROM investment_events WHERE linked_trx_id=?",
                     (yret[0]["id"],)).fetchone()
    assert yev and yev["kind"] == "contribution" \
        and abs(yev["amount"] - 1800.00) < 0.005 \
        and yev["event_date"] == "2026-06-30"
    ok("ytd apply: standalone rows tie; $1800 YTD contribution synced")

    # ── 7) Guardrail: a draft that doesn't tie refuses to execute ────────
    r = c.post("/tools/payroll/draft", data={
        "entry_mode": "manual", "pay_date": "2026-04-05",
        "gross_earnings": "5000.00", "tax_federal": "600.00",
        "net_pay": "3000.00",   # off by 1400 with no plug
    }, follow_redirects=True)
    assert r.status_code == 200
    brec = db.execute("""SELECT * FROM payroll_reconciliations
                          WHERE entry_mode='manual' AND pay_date='2026-04-05'
                       """).fetchone()
    d = c.post(f"/tools/payroll/{brec['id']}/execute").get_json()
    assert not d.get("ok") and "tie" in (d.get("error") or "").lower()
    ok("guardrail: non-tying draft blocked from executing")

    # ── 8) Pages render ──────────────────────────────────────────────────
    # Upload flow end-to-end (also archives the CSV to imports/payroll/)
    r = c.post("/tools/payroll/upload", data={
        "csv": (io.BytesIO(GUSTO_CSV.encode()), "test-payroll-journal.csv")},
        content_type="multipart/form-data")
    assert r.status_code == 200 and b"2026-01-31" in r.data
    for url in ["/tools/payroll", "/tools/payroll?status=all",
                "/tools/payroll/manual",
                f"/tools/payroll/{mrec['id']}",   # reconciled view
                f"/tools/payroll/{brec['id']}"]:  # draft view
        assert c.get(url).status_code == 200, url
    ok("pages: home, manual true-up form, views + upload review render 200")

    # ── 9) Gusto parse: Roth + Garnishment columns bucket as post-tax ────
    # Roth must NOT hit pretax_retirement even though "401(k)" is a
    # retirement hint; garnishment is its own post-tax bucket.
    hdr2 = ("Last Name,First Name,Gross Earnings,"
            "Traditional 401(k) (Employee Deduction),"
            "Roth 401(k) (Employee Deduction),"
            "Garnishment (Employee Deduction),"
            "Federal Income Tax (Employee),Social Security (Employee),"
            "Medicare (Employee),CA Withholding Tax (Employee),Net Pay,"
            "Social Security (Employer),Roth 401(k) (Company Contribution)")
    # 5000 − 250 − 150 − 75 − 600 − 310 − 72.50 − 200 = 3342.50
    csv2 = (f"Payroll period, 02/01/2026 - 02/15/2026\n"
            f"Pay day, 02/20/2026\n{hdr2}\n"
            "Doe,Jane,5000.00,250.00,150.00,75.00,600.00,310.00,72.50,"
            "200.00,3342.50,310.00,150.00\n"
            "Payroll Totals,,5000.00,250.00,150.00,75.00,600.00,310.00,"
            "72.50,200.00,3342.50,310.00,150.00\n")
    (pp,) = rp.parse_gusto_csv(csv2)
    assert abs(pp["pretax_retirement"] - 250.00) < 0.005
    assert abs(pp["posttax_roth"] - 150.00) < 0.005, \
        "Roth column must bucket as posttax_roth, not pretax_retirement"
    assert abs(pp["posttax_garnish"] - 75.00) < 0.005
    assert pp["net_pay_matches"], \
        "tie math must include post-tax lines (employer Roth ignored)"
    ok("gusto parse: Roth → posttax_roth, Garnishment → posttax_garnish, ties")

    # ── 10) Manual apply with Roth + garnishment ─────────────────────────
    # 6000 − 700 − 459 − 300 (pretax ret) − 250 (Roth) − 100 (garnish)
    # = 4191.00. Roth books as a SECOND transfer sharing the retirement
    # target; garnishment books as contra-income under its own L2.
    r = c.post("/tools/payroll/draft", data={
        "entry_mode": "manual", "pay_date": "2026-05-05",
        "gross_earnings": "6000.00", "tax_federal": "700.00",
        "tax_ss": "459.00", "pretax_retirement": "300.00",
        "posttax_roth": "250.00", "posttax_garnish": "100.00",
        "net_pay": "4191.00",
    }, follow_redirects=True)
    assert r.status_code == 200
    prec = db.execute("""SELECT * FROM payroll_reconciliations
                          WHERE entry_mode='manual' AND pay_date='2026-05-05'
                       """).fetchone()
    assert prec and abs(prec["posttax_roth"] - 250.00) < 0.005 \
        and abs(prec["posttax_garnish"] - 100.00) < 0.005
    plan = rp.plan_payroll_splits(prec, rp.resolve_retirement_target(db, prec))
    assert plan["ties"], plan
    d = c.post(f"/tools/payroll/{prec['id']}/execute").get_json()
    assert d.get("ok"), d
    prows = db.execute("""
        SELECT t.* FROM transactions t
          JOIN payroll_reconciliation_trxs a ON a.trx_id = t.id
         WHERE a.reconciliation_id=? AND a.role='standalone'
    """, (prec["id"],)).fetchall()
    assert abs(sum(t["amount"] for t in prows) - 4191.00) < 0.01, \
        "rows must tie: gross − taxes − pretax − posttax = net"
    xfers = sorted([t for t in prows if t["trx_type"] == "transfer"],
                   key=lambda t: t["amount"])
    assert len(xfers) == 2, "pre-tax AND Roth must each book a transfer row"
    assert abs(xfers[0]["amount"] - (-300.00)) < 0.005   # pre-tax
    assert abs(xfers[1]["amount"] - (-250.00)) < 0.005   # Roth
    assert all(t["l2_category"] == "My 401k" for t in xfers), \
        "both transfers share the same target account"
    garn = [t for t in prows if t["l1_category"] == "Post-Tax Deductions"]
    assert len(garn) == 1 and garn[0]["trx_type"] == "income" \
        and abs(garn[0]["amount"] - (-100.00)) < 0.005 \
        and garn[0]["l2_category"] == "Garnishments & Other", \
        "garnishment books as contra-income under its own L2"
    ok("manual apply: Roth transfer + garnishment contra-income; tie holds")

    # ── 10b) Roth investment event synced; undo reverses everything ──────
    roth_ev = db.execute("SELECT * FROM investment_events WHERE linked_trx_id=?",
                         (xfers[1]["id"],)).fetchone()
    assert roth_ev and roth_ev["kind"] == "contribution" \
        and abs(roth_ev["amount"] - 250.00) < 0.005 \
        and roth_ev["event_date"] == "2026-05-05" \
        and roth_ev["account_id"] == inv_acct_id, \
        "Roth transfer must sync a contribution event like the pre-tax path"
    pre_ev = db.execute("SELECT * FROM investment_events WHERE linked_trx_id=?",
                        (xfers[0]["id"],)).fetchone()
    assert pre_ev and abs(pre_ev["amount"] - 300.00) < 0.005, \
        "pre-tax transfer still syncs its own event alongside Roth"
    d = c.post(f"/tools/payroll/{prec['id']}/undo").get_json()
    assert d.get("ok"), d
    assert db.execute("SELECT COUNT(*) n FROM transactions t "
                      "JOIN payroll_reconciliation_trxs a ON a.trx_id=t.id "
                      "WHERE a.reconciliation_id=?",
                      (prec["id"],)).fetchone()["n"] == 0
    # Both of THIS rec's events (pre-tax + Roth) are gone; the still-
    # reconciled YTD entry's $1800 event from section 6 must survive.
    assert db.execute("SELECT COUNT(*) n FROM investment_events "
                      "WHERE id IN (?,?)",
                      (pre_ev["id"], roth_ev["id"])).fetchone()["n"] == 0
    remaining = db.execute("SELECT * FROM investment_events "
                           "WHERE account_id=?", (inv_acct_id,)).fetchall()
    assert len(remaining) == 1 and abs(remaining[0]["amount"] - 1800.00) < 0.005, \
        "only the untouched YTD contribution may remain"
    ok("undo: Roth + garnishment rows deleted, both investment events removed")

    # ── 11) Tie guardrail includes post-tax amounts ──────────────────────
    r = c.post("/tools/payroll/draft", data={
        "entry_mode": "manual", "pay_date": "2026-05-20",
        "gross_earnings": "1000.00", "posttax_garnish": "100.00",
        "net_pay": "950.00",   # math says 900 — off by 50
    }, follow_redirects=True)
    assert r.status_code == 200
    trec = db.execute("""SELECT * FROM payroll_reconciliations
                          WHERE entry_mode='manual' AND pay_date='2026-05-20'
                       """).fetchone()
    d = c.post(f"/tools/payroll/{trec['id']}/execute").get_json()
    assert not d.get("ok") and "tie" in (d.get("error") or "").lower(), \
        "post-tax amounts must participate in the tie check"
    ok("guardrail: tie check counts post-tax lines")

    print(f"\nPAYROLL TESTS PASSED — {PASS} checks green")


if __name__ == "__main__":
    main()
