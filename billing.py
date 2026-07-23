"""
billing.py — credit-card statement-date math, driven by the accounts table.

Each credit-card account carries its own billing days (set on Docs &
Settings → Accounts):

    stmt_close_day  — day of month the statement cycle closes (1–31,
                      REQUIRED for credit-card accounts). For 29–31,
                      months without that day close on the month's last
                      day (EOM clamp).
    pay_due_day     — day of month the payment is due (optional).

Convention: a charge POSTING strictly before the close day lands on this
month's statement; a charge posting on/after the close day rolls to next
month's. The due date falls in the same month as the close when
pay_due_day > stmt_close_day, otherwise in the following month.

For accounts WITHOUT billing days (checking, savings, wallets — or a
credit card whose close day isn't set yet), the money posts the same day
it's available: statement/payment dates collapse to the post date so
every cash-basis rollup can use COALESCE(payment_date, trx_date) without
special-casing.
"""
import calendar
from datetime import date


def add_months(d, n):
    """Add n months to a date, keeping day fixed (caller clamps)."""
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def clamp_day(year, month, day):
    """date(year, month, day) with EOM clamping: day 31 in a 30-day month
    → the 30th; Feb 29–31 → the 28th/29th."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def calc_dates_for_close(post_date_str, close_day, due_day=None):
    """Pure calc: (statement_date, payment_date) for a post date given the
    card's close day (+ optional due day). No DB access.

    - post day < close day  → statement closes THIS month (on close_day,
      EOM-clamped);
    - post day ≥ close day  → rolls to NEXT month's close.
    - payment due: same month as the close when due_day > close_day,
      else the following month. None when due_day isn't set.
    """
    if not post_date_str or not close_day:
        return None, None
    try:
        d = date.fromisoformat(str(post_date_str)[:10])
        close_day = int(close_day)
    except (ValueError, TypeError):
        return None, None
    if not (1 <= close_day <= 31):
        return None, None

    cutoff = close_day - 1          # last post day on this month's statement
    if d.day <= cutoff:
        stmt = clamp_day(d.year, d.month, close_day)
    else:
        nxt = add_months(date(d.year, d.month, 1), 1)
        stmt = clamp_day(nxt.year, nxt.month, close_day)

    pay = None
    if due_day:
        try:
            due_day = int(due_day)
        except (ValueError, TypeError):
            due_day = None
        if due_day and 1 <= due_day <= 31:
            pay_offset = 0 if due_day > close_day else 1
            pm = add_months(date(stmt.year, stmt.month, 1), pay_offset)
            pay = clamp_day(pm.year, pm.month, due_day)
    return stmt.isoformat(), (pay.isoformat() if pay else None)


def account_billing(db, account_num=None, account_id=None):
    """Look up (close_day, due_day, is_credit_card) for an account by
    account_num or id. (None, None, False) when not found."""
    if account_id is not None:
        row = db.execute(
            "SELECT type, stmt_close_day, pay_due_day FROM accounts WHERE id=?",
            (account_id,)).fetchone()
    else:
        row = db.execute(
            "SELECT type, stmt_close_day, pay_due_day FROM accounts "
            "WHERE account_num=?", (str(account_num),)).fetchone()
    if not row:
        return None, None, False
    return row["stmt_close_day"], row["pay_due_day"], row["type"] == "credit_card"


def calc_payment_dates(db, post_date_str, account_num):
    """
    DB-driven (statement_date, payment_date) for a post date and account.

    Credit-card account with a close day set → real statement math (see
    calc_dates_for_close). Credit card WITHOUT a close day, or any non-CC
    account → dates collapse to the post date (the historical fallback),
    so date fields are 'complete' rather than blank.
    """
    if not post_date_str:
        return None, None
    close_day, due_day, is_cc = account_billing(db, account_num=account_num)
    if not (is_cc and close_day):
        return post_date_str, post_date_str
    stmt, pay = calc_dates_for_close(post_date_str, close_day, due_day)
    if stmt is None:
        return None, None
    return stmt, pay
