"""
investments_returns.py — returns math over the lot/event engine
(2026-07-05 build; see investments_engine.py).

Everything reads from investment_events + investment_lots. Sign
conventions here are the INVESTOR's (cash-flow) perspective:
  money you put in  → negative flow
  money you get out → positive flow
  terminal (current) value → positive flow at the end

XIRR is money-weighted return — "given WHEN each dollar went in/out,
what compounded annual rate did I earn?" — the timing-adjusted view.
TWR (time-weighted) is computed per account when at least two
snapshots exist; it strips out flow timing (fund-manager view) and
complements XIRR.
"""

from __future__ import annotations
from datetime import date, datetime
from typing import List, Tuple, Optional, Dict, Any

DAY = 365.25


def _d(s) -> date:
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


# ─── XIRR ─────────────────────────────────────────────────────────────────

def xirr(flows: List[Tuple[Any, float]]) -> Optional[float]:
    """Annualized money-weighted return for dated flows
    [(date, amount), ...] — investor perspective (in = −, out/value = +).
    Bisection on NPV(rate) over (−99.99%, +1000%); robust, no derivative.
    Returns None when undefined (fewer than 2 flows, all one sign, or no
    sign change in range)."""
    if len(flows) < 2:
        return None
    fl = sorted(((_d(dt), float(a)) for dt, a in flows), key=lambda x: x[0])
    if all(a >= 0 for _, a in fl) or all(a <= 0 for _, a in fl):
        return None
    t0 = fl[0][0]

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** (((d - t0).days) / DAY) for d, a in fl)

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


# ─── Flow extraction ──────────────────────────────────────────────────────

# Event kinds that are FLOWS from the account's perspective; everything
# else (snapshot / dividend / interest / fee / gain_loss / closure) is a
# value change inside the account, not investor cash.
_ACCOUNT_FLOW_SIGN = {
    "contribution": -1,   # investor puts money in
    "lot_move_in":  -1,   # money arrives INTO this account
    "withdrawal":   +1,   # investor takes money out
    "lot_move_out": +1,   # money leaves this account
}
_EXTERNAL_KINDS = ("contribution", "withdrawal")   # global view: moves cancel


def account_flows(db, account_id: int) -> List[Tuple[str, float]]:
    out = []
    for r in db.execute("""
        SELECT event_date, kind, amount FROM investment_events
         WHERE account_id=? AND kind IN ('contribution','withdrawal',
                                         'lot_move_in','lot_move_out')
         ORDER BY event_date, id""", (account_id,)):
        sign = _ACCOUNT_FLOW_SIGN[r["kind"]]
        out.append((r["event_date"], sign * abs(float(r["amount"] or 0))))
    return out


def global_flows(db) -> List[Tuple[str, float]]:
    """External flows only — inter-investment moves cancel by definition."""
    out = []
    for r in db.execute("""
        SELECT event_date, kind, amount FROM investment_events
         WHERE kind IN ('contribution','withdrawal')
         ORDER BY event_date, id"""):
        sign = -1 if r["kind"] == "contribution" else +1
        out.append((r["event_date"], sign * abs(float(r["amount"] or 0))))
    return out


def account_xirr(db, account_id: int, current_value: float,
                 as_of: Optional[date] = None) -> Optional[float]:
    fl = account_flows(db, account_id)
    if current_value > 1e-9:
        fl.append(((as_of or date.today()).isoformat(), current_value))
    return xirr(fl)


def global_xirr(db, total_value: float,
                as_of: Optional[date] = None) -> Optional[float]:
    fl = global_flows(db)
    if total_value > 1e-9:
        fl.append(((as_of or date.today()).isoformat(), total_value))
    return xirr(fl)


# ─── TWR (per account, needs ≥2 snapshots) ────────────────────────────────

def account_twr(db, account_id: int) -> Optional[Dict[str, float]]:
    """Time-weighted return, chained across snapshot periods using MODIFIED
    DIETZ for each sub-period so large mid-period flows (e.g. a rollover) don't
    distort it:

        r_i = (V_end − V_start − F) / (V_start + Σ wᵢ·Fᵢ)

    where F is total net flow in the period and wᵢ = fraction of the period a
    flow was invested. This weights the denominator by average capital rather
    than the (possibly tiny) starting value. Returns {'total','annualized'} or
    None (<2 snapshots)."""
    snaps = db.execute("""
        SELECT event_date, snapshot_value FROM investment_events
         WHERE account_id=? AND kind='snapshot'
         ORDER BY event_date, id""", (account_id,)).fetchall()
    if len(snaps) < 2:
        return None
    growth = 1.0
    for a, b in zip(snaps, snaps[1:]):
        v0, v1 = float(a["snapshot_value"]), float(b["snapshot_value"])
        p0, p1 = _d(a["event_date"]), _d(b["event_date"])
        pdays = (p1 - p0).days or 1
        flows = db.execute("""
            SELECT event_date, kind, amount FROM investment_events
             WHERE account_id=? AND event_date > ? AND event_date <= ?
               AND kind IN ('contribution','withdrawal','lot_move_in','lot_move_out')
        """, (account_id, a["event_date"], b["event_date"])).fetchall()
        F, weighted = 0.0, 0.0
        for f in flows:
            amt = abs(float(f["amount"] or 0))
            signed = amt if f["kind"] in ("contribution", "lot_move_in") else -amt
            F += signed
            w = (p1 - _d(f["event_date"])).days / pdays   # time still invested
            weighted += signed * w
        denom = v0 + weighted
        if denom <= 0:
            continue   # can't measure this sub-period; skip rather than blow up
        growth *= (1.0 + (v1 - v0 - F) / denom)
    days = (_d(snaps[-1]["event_date"]) - _d(snaps[0]["event_date"])).days
    total = growth - 1.0
    ann = (growth ** (DAY / days) - 1.0) if days > 0 and growth > 0 else None
    return {"total": total, "annualized": ann}


# ─── Lot metrics ──────────────────────────────────────────────────────────

def lot_metrics(lot, as_of: Optional[date] = None) -> Dict[str, Any]:
    """Per-lot 'what did each $10K do' numbers: absolute gain, total
    return on origin, and CAGR from origin_date to today (open lots) or
    closed_at (closed lots)."""
    origin = float(lot["origin_amount"] or 0)
    value = float(lot["current_value"] or 0)
    end = _d(lot["closed_at"]) if lot["closed_at"] else (as_of or date.today())
    days = max((end - _d(lot["origin_date"])).days, 0)
    gain = value - origin
    total = (gain / origin) if origin > 0 else None
    cagr = None
    if origin > 0 and value > 0 and days >= 30:
        cagr = (value / origin) ** (DAY / days) - 1.0
    return {"gain": gain, "total_return": total, "cagr": cagr,
            "years": days / DAY}


# ─── Snapshot series (charts) ─────────────────────────────────────────────

def portfolio_series(db, current_total: float) -> List[Tuple[str, float]]:
    """(date, total_value) points for the value-over-time chart: at each
    snapshot date, sum the latest-known snapshot value per account as of
    that date; append (today, current_total from live lots)."""
    rows = db.execute("""
        SELECT event_date, account_id, snapshot_value FROM investment_events
         WHERE kind='snapshot' ORDER BY event_date, id""").fetchall()
    if not rows:
        return ([(date.today().isoformat(), current_total)]
                if current_total > 0 else [])
    # Accounts that closed (money rolled out / cashed out) must STOP counting
    # after their close date — otherwise a rolled-over account (e.g. an old
    # employer 401K → new 401K) is double-counted: once via its own stale last
    # snapshot, and again inside the destination account's snapshot.
    closed = {r["id"]: r["closed_date"] for r in db.execute(
        "SELECT id, closed_date FROM accounts "
        "WHERE type='investment' AND closed_date IS NOT NULL")}
    dates = sorted({r["event_date"] for r in rows})
    latest: Dict[int, float] = {}
    series = []
    for dte in dates:
        for r in rows:
            if r["event_date"] <= dte:
                latest[r["account_id"]] = float(r["snapshot_value"])
        total = sum(v for aid, v in latest.items()
                    if not (closed.get(aid) and closed[aid] <= dte))
        series.append((dte, total))
    # The snapshot-sum points approximate the trend, but they miss accounts
    # with no snapshot (the alts) and deposits made after an account's last
    # snapshot. Anchor the FINAL point to the true current total (sum of open
    # lots) so the chart ends where the cards do — replace the last point if
    # it's today, else append.
    today = date.today().isoformat()
    if series and series[-1][0] >= today:
        series[-1] = (series[-1][0], current_total)
    else:
        series.append((today, current_total))
    return series


def employer_summary(db) -> Dict[str, Any]:
    """Employer money rollup — answers 'how much have employers given me,
    and what has it grown to?' Reads OPEN lots tagged source='employer'
    (money still invested), grouped by contribution year and account.

    NOTE: based on currently-open employer lots, so 'given' == origin still
    invested. If employer money were ever withdrawn, that portion drops out
    (fine for the 'what do I still hold from employers' question). Returns
    {given, value, gain, n, rows:[{year, account, given, value, gain}]}.
    """
    lots = db.execute("""
        SELECT l.id, l.origin_date, l.origin_amount, l.current_value,
               a.name AS account_name
          FROM investment_lots l
          JOIN accounts a ON a.id = l.current_account_id
         WHERE l.source='employer' AND l.closed_at IS NULL
         ORDER BY l.origin_date, l.id
    """).fetchall()

    def _origin_account(lot_id):
        """Walk parent_lot_id back to the root lot — the account where this
        money was FIRST contributed (e.g. Employer Trad 401K), before any rollover
        moved it to where it lives now."""
        cur = lot_id
        for _ in range(200):  # guard against cycles
            r = db.execute("SELECT parent_lot_id, current_account_id FROM investment_lots WHERE id=?",
                           (cur,)).fetchone()
            if r is None:
                return None
            if r["parent_lot_id"] is None:
                nm = db.execute("SELECT name FROM accounts WHERE id=?",
                                (r["current_account_id"],)).fetchone()
                return nm["name"] if nm else None
            cur = r["parent_lot_id"]
        return None

    given = sum(float(l["origin_amount"] or 0) for l in lots)
    value = sum(float(l["current_value"] or 0) for l in lots)
    agg: Dict[Tuple[str, str, str], List[float]] = {}
    for l in lots:
        origin_acct = _origin_account(l["id"]) or l["account_name"]
        key = (str(l["origin_date"])[:4], origin_acct, l["account_name"])
        cell = agg.setdefault(key, [0.0, 0.0])
        cell[0] += float(l["origin_amount"] or 0)
        cell[1] += float(l["current_value"] or 0)
    rows = [{"year": k[0], "original_account": k[1], "current_account": k[2],
             "given": v[0], "value": v[1], "gain": v[1] - v[0]}
            for k, v in sorted(agg.items())]
    return {"given": given, "value": value, "gain": value - given,
            "n": len(lots), "rows": rows}


def contributions_series(db) -> List[Tuple[str, float]]:
    """(date, cumulative net external contributions) for the chart's
    'money in' line."""
    out, cum = [], 0.0
    for dte, amt in global_flows(db):
        cum += -amt          # contribution flows are negative → money in
        out.append((dte, cum))
    return out
