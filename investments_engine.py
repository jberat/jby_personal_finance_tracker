"""
Investments Engine — lot/event investment tracking.

The event-sourced layer that sits on top of the `investment_lots` and
`investment_events` tables. Routes and migration scripts call into this
module rather than writing raw SQL — it keeps lot-state maintenance
consistent with every event written.

Conceptual model, quick summary:

  - investment_lots = materialized state. Each lot has a sticky
    origin_date + origin_amount (the original external $ that entered
    the investment universe) and a mutable current_value /
    current_account_id. Lots can split (partial withdrawals / partial
    moves) and carry origin metadata across accounts via parent_lot_id.

  - investment_events = append-only ledger. Every state change writes
    a row. Sign convention for events.amount is from the account's POV:
      contribution +X / withdrawal -X
      lot_move_in +X / lot_move_out -X
      dividend +X / interest +X / fee -X / gain_loss signed
      snapshot — amount NULL, snapshot_value carries the absolute value
      closure  — amount NULL (informational; flow is in lot_moves)

CALLER OWNS THE TRANSACTION. Each function writes multiple rows with
no internal commit, so the caller can compose multiple engine calls in
one atomic transaction (e.g. "create cash-side trx + contribution event"
in a route handler). Caller calls db.commit() at the outer scope.
"""

from __future__ import annotations
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, date


def _pdate(s) -> date:
    """Parse a YYYY-MM-DD (or date) into a date."""
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


# ─── Validation helpers ───────────────────────────────────────────────────

_ADJUSTMENT_KINDS = {"dividend", "interest", "fee", "gain_loss"}
_ALL_KINDS = {
    "contribution", "withdrawal",
    "lot_move_out", "lot_move_in",
    "snapshot",
    "dividend", "interest", "fee", "gain_loss",
    "closure",
}


def _require(cond: bool, msg: str):
    if not cond:
        raise ValueError(msg)


def _is_investment_account(db, account_id: int) -> bool:
    row = db.execute(
        "SELECT type FROM accounts WHERE id=?", (account_id,)
    ).fetchone()
    return bool(row and row["type"] == "investment")


# ─── Read helpers ─────────────────────────────────────────────────────────

def _has_any_lots(db, account_id: int) -> bool:
    """True if the account has EVER held a lot (open or closed) — i.e. it was
    funded at some point. Distinguishes 'drained account with a tiny residual'
    (legit) from 'never funded, snapshot before any contribution' (a mistake)."""
    return db.execute(
        "SELECT 1 FROM investment_lots WHERE current_account_id=? LIMIT 1",
        (account_id,)).fetchone() is not None


def get_open_lots(db, account_id: int) -> List[Dict[str, Any]]:
    """All currently open lots (closed_at IS NULL) in an account, ordered
    by origin_date ascending (oldest first — for FIFO consumption)."""
    rows = db.execute("""
        SELECT * FROM investment_lots
        WHERE current_account_id = ? AND closed_at IS NULL
        ORDER BY origin_date ASC, id ASC
    """, (account_id,)).fetchall()
    return [dict(r) for r in rows]


def account_value(db, account_id: int) -> float:
    """Sum of current_value across all open lots in this account."""
    row = db.execute("""
        SELECT COALESCE(SUM(current_value), 0) AS v
        FROM investment_lots
        WHERE current_account_id = ? AND closed_at IS NULL
    """, (account_id,)).fetchone()
    return float(row["v"] or 0.0)


def account_principal(db, account_id: int) -> float:
    """Sum of origin_amount across all open lots in this account.
    This is "principal sitting in the account right now" — the amount
    of original external $ currently held here. Differs from lifetime
    contributions because lots can move out."""
    row = db.execute("""
        SELECT COALESCE(SUM(origin_amount), 0) AS p
        FROM investment_lots
        WHERE current_account_id = ? AND closed_at IS NULL
    """, (account_id,)).fetchone()
    return float(row["p"] or 0.0)


# ─── Internal write helpers ───────────────────────────────────────────────

def _write_event(db, *, event_date: str, account_id: int, kind: str,
                 amount: Optional[float] = None,
                 lot_id: Optional[int] = None,
                 paired_event_id: Optional[int] = None,
                 linked_trx_id: Optional[int] = None,
                 snapshot_value: Optional[float] = None,
                 note: Optional[str] = None,
                 extra_json: Optional[str] = None) -> int:
    """Insert an investment_events row. Returns event id.
    Performs no lot-state mutation — that's the caller's job."""
    _require(kind in _ALL_KINDS, f"Unknown event kind: {kind!r}")
    cur = db.execute("""
        INSERT INTO investment_events
            (event_date, account_id, kind, amount, lot_id,
             paired_event_id, linked_trx_id, snapshot_value, note, extra_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (event_date, account_id, kind, amount, lot_id,
          paired_event_id, linked_trx_id, snapshot_value, note, extra_json))
    return cur.lastrowid


def _create_lot(db, *, current_account_id: int, origin_date: str,
                origin_amount: float, current_value: float,
                parent_lot_id: Optional[int] = None,
                origin_event_id: Optional[int] = None,
                note: Optional[str] = None,
                source: str = "you") -> int:
    """Insert an investment_lots row. `source` ∈ 'you' | 'employer' and
    travels with the money (inherited across splits and moves). Returns lot id."""
    cur = db.execute("""
        INSERT INTO investment_lots
            (current_account_id, origin_date, origin_amount, current_value,
             parent_lot_id, origin_event_id, note, source)
        VALUES (?,?,?,?,?,?,?,?)
    """, (current_account_id, origin_date, origin_amount, current_value,
          parent_lot_id, origin_event_id, note,
          "employer" if source == "employer" else "you"))
    return cur.lastrowid


def _close_lot(db, lot_id: int, *, closed_at: str,
               closing_event_id: int) -> None:
    db.execute("""
        UPDATE investment_lots
           SET closed_at = ?, closing_event_id = ?
         WHERE id = ?
    """, (closed_at, closing_event_id, lot_id))


def _set_lot_value(db, lot_id: int, current_value: float) -> None:
    db.execute("UPDATE investment_lots SET current_value = ? WHERE id = ?",
               (current_value, lot_id))


def _split_lot(db, lot: Dict[str, Any], *, consumed_value: float,
               event_date: str, closing_event_id: int,
               note: Optional[str] = None) -> Tuple[int, int]:
    """Proportionally split an open lot into a closed (consumed) remnant
    and a continuing (remainder) lot.

    Allocation is by current_value: if you consume X% of the value, X%
    of the origin_amount goes with the consumed remnant. This preserves
    the lot's gain/loss ratio across both children.

    Returns (consumed_lot_id, remainder_lot_id). The original lot is
    closed and zeroed out; both new lots inherit parent_lot_id = original.
    The CALLER is responsible for any subsequent move/withdrawal action
    on the consumed_lot_id.
    """
    cur_val = float(lot["current_value"])
    _require(cur_val > 0, "Cannot split a zero-value lot")
    _require(0 < consumed_value <= cur_val,
             f"consumed_value {consumed_value} out of range (lot value {cur_val})")

    ratio = consumed_value / cur_val
    consumed_origin = float(lot["origin_amount"]) * ratio
    remainder_origin = float(lot["origin_amount"]) - consumed_origin
    remainder_value = cur_val - consumed_value

    _src = lot.get("source") or "you"
    consumed_id = _create_lot(
        db,
        current_account_id=lot["current_account_id"],
        origin_date=lot["origin_date"],
        origin_amount=consumed_origin,
        current_value=consumed_value,
        parent_lot_id=lot["id"],
        origin_event_id=lot.get("origin_event_id"),
        note=f"Split from lot {lot['id']} (consumed)" + (f"; {note}" if note else ""),
        source=_src,
    )
    remainder_id = _create_lot(
        db,
        current_account_id=lot["current_account_id"],
        origin_date=lot["origin_date"],
        origin_amount=remainder_origin,
        current_value=remainder_value,
        parent_lot_id=lot["id"],
        origin_event_id=lot.get("origin_event_id"),
        note=f"Split from lot {lot['id']} (remainder)",
        source=_src,
    )
    # Close the original lot (its halves now live in the two new lots).
    _close_lot(db, lot["id"], closed_at=event_date,
               closing_event_id=closing_event_id)
    # Zero the original's current_value so summed views still reconcile
    # if anyone queries it directly.
    _set_lot_value(db, lot["id"], 0.0)
    return consumed_id, remainder_id


def _consume_lots_fifo(db, account_id: int, target_amount: float,
                       event_date: str, closing_event_id: int,
                       note_prefix: str = "") -> List[int]:
    """Walk open lots in FIFO order, closing whole ones and splitting
    the last partial one as needed to consume exactly target_amount of
    current value. Returns the list of lot ids that were consumed (may
    include remnants from splits)."""
    consumed_ids: List[int] = []
    remaining = target_amount
    for lot in get_open_lots(db, account_id):
        if remaining <= 1e-9:
            break
        lot_val = float(lot["current_value"])
        if lot_val <= 1e-9:
            continue
        if lot_val <= remaining + 1e-9:
            # Close whole lot
            _close_lot(db, lot["id"], closed_at=event_date,
                       closing_event_id=closing_event_id)
            consumed_ids.append(lot["id"])
            remaining -= lot_val
        else:
            # Split lot: consume `remaining`, leave the rest open.
            # _split_lot closes the original lot and creates two children
            # (consumed + remainder). The consumed child is born OPEN; we
            # close it here so the withdrawal actually retires that value.
            consumed_id, _remainder_id = _split_lot(
                db, lot, consumed_value=remaining,
                event_date=event_date, closing_event_id=closing_event_id,
                note=note_prefix or None,
            )
            _close_lot(db, consumed_id, closed_at=event_date,
                       closing_event_id=closing_event_id)
            consumed_ids.append(consumed_id)
            remaining = 0.0
            break
    _require(remaining < 1e-6,
             f"Insufficient open-lot value in account {account_id} to "
             f"consume {target_amount} (short by {remaining})")
    return consumed_ids


# ─── Public API ───────────────────────────────────────────────────────────

def record_contribution(db, *, account_id: int, event_date: str,
                        amount: float, linked_trx_id: Optional[int] = None,
                        note: Optional[str] = None,
                        source: str = "you") -> Tuple[int, int]:
    """Record an external → investment-account contribution.
    Creates a new lot (origin_date = event_date, origin_amount = amount,
    current_value = amount) and writes a contribution event linked to it.
    `source` ∈ 'you' (your own capital) | 'employer' (an employer match
    that came straight from the company, never through you).
    Returns (event_id, lot_id)."""
    _require(amount > 0, "Contribution amount must be positive")
    _require(_is_investment_account(db, account_id),
             f"Account {account_id} is not an investment account")

    # Create lot first so we can link event.lot_id; backfill origin_event_id after.
    lot_id = _create_lot(
        db,
        current_account_id=account_id,
        origin_date=event_date,
        origin_amount=amount,
        current_value=amount,
        note=note,
        source=source,
    )
    event_id = _write_event(
        db, event_date=event_date, account_id=account_id,
        kind="contribution", amount=amount,
        lot_id=lot_id, linked_trx_id=linked_trx_id, note=note,
    )
    db.execute("UPDATE investment_lots SET origin_event_id = ? WHERE id = ?",
               (event_id, lot_id))
    return event_id, lot_id


def record_withdrawal(db, *, account_id: int, event_date: str,
                      amount: float, linked_trx_id: Optional[int] = None,
                      note: Optional[str] = None) -> int:
    """Record a withdrawal from an investment account back out to
    external. FIFO-consumes open lots (splitting the last one if needed)
    until `amount` of current value has been removed. Returns event_id.

    Sign convention: amount is supplied positive; event row stores it
    as -amount (account loses).
    """
    _require(amount > 0, "Withdrawal amount must be positive")
    _require(_is_investment_account(db, account_id),
             f"Account {account_id} is not an investment account")

    event_id = _write_event(
        db, event_date=event_date, account_id=account_id,
        kind="withdrawal", amount=-amount,
        linked_trx_id=linked_trx_id, note=note,
    )
    _consume_lots_fifo(db, account_id, amount, event_date, event_id,
                       note_prefix="withdrawal")
    return event_id


def record_lot_move(db, *, src_account_id: int, dst_account_id: int,
                    event_date: str, amount: float,
                    note: Optional[str] = None) -> Tuple[int, int]:
    """Record an inter-investment transfer (e.g. Trad IRA → Roth IRA,
    CD → brokerage). FIFO-consumes lots from the source up to `amount`,
    splitting if needed. For each consumed source lot, creates a paired
    destination lot inheriting origin_date + origin_amount (carrying
    principal vs. gains across the move).

    Writes paired events: lot_move_out (negative on src) + lot_move_in
    (positive on dst), linked via paired_event_id.

    Returns (out_event_id, in_event_id).
    """
    _require(amount > 0, "Move amount must be positive")
    _require(src_account_id != dst_account_id,
             "Source and destination must differ")
    _require(_is_investment_account(db, src_account_id),
             f"Source account {src_account_id} is not an investment account")
    _require(_is_investment_account(db, dst_account_id),
             f"Destination account {dst_account_id} is not an investment account")

    out_event_id = _write_event(
        db, event_date=event_date, account_id=src_account_id,
        kind="lot_move_out", amount=-amount, note=note,
    )
    in_event_id = _write_event(
        db, event_date=event_date, account_id=dst_account_id,
        kind="lot_move_in", amount=amount,
        paired_event_id=out_event_id, note=note,
    )
    # Backfill paired link the other direction
    db.execute("UPDATE investment_events SET paired_event_id=? WHERE id=?",
               (in_event_id, out_event_id))

    # Walk source lots FIFO, consuming `amount` of current value. For
    # each consumed source lot (or split fragment), create a matching
    # destination lot carrying its origin metadata.
    remaining = amount
    for lot in get_open_lots(db, src_account_id):
        if remaining <= 1e-9:
            break
        lot_val = float(lot["current_value"])
        if lot_val <= 1e-9:
            continue
        if lot_val <= remaining + 1e-9:
            consumed_origin = float(lot["origin_amount"])
            consumed_value  = lot_val
            _close_lot(db, lot["id"], closed_at=event_date,
                       closing_event_id=out_event_id)
            remaining -= consumed_value
        else:
            consumed_id, _rem_id = _split_lot(
                db, lot, consumed_value=remaining,
                event_date=event_date, closing_event_id=out_event_id,
                note="lot_move source split",
            )
            consumed_lot = db.execute(
                "SELECT * FROM investment_lots WHERE id=?", (consumed_id,)
            ).fetchone()
            consumed_origin = float(consumed_lot["origin_amount"])
            consumed_value  = float(consumed_lot["current_value"])
            # Close the consumed split fragment too — its value migrates to dest.
            _close_lot(db, consumed_id, closed_at=event_date,
                       closing_event_id=out_event_id)
            remaining = 0.0
        # Create destination lot inheriting origin metadata AND source
        # (employer money stays employer money after a move).
        _create_lot(
            db,
            current_account_id=dst_account_id,
            origin_date=lot["origin_date"],
            origin_amount=consumed_origin,
            current_value=consumed_value,
            parent_lot_id=lot["id"],
            origin_event_id=in_event_id,
            note=f"From lot {lot['id']} via move on {event_date}"
                 + (f"; {note}" if note else ""),
            source=lot.get("source") or "you",
        )
        if remaining <= 1e-9:
            break

    _require(remaining < 1e-6,
             f"Insufficient open-lot value in source account {src_account_id} "
             f"to move {amount} (short by {remaining})")
    return out_event_id, in_event_id


def record_adjustment(db, *, account_id: int, event_date: str,
                      kind: str, amount: float,
                      note: Optional[str] = None) -> int:
    """Record a value-adjustment event (dividend / interest / fee /
    gain_loss) and proportionally distribute the amount across all
    open lots in the account, scaled by current_value.

    Sign:
      dividend, interest    — caller passes positive amount
      fee                   — caller passes positive amount; stored negative
      gain_loss             — caller passes signed amount (+ gain, − loss)

    Origin amounts are NEVER touched by adjustments — only current_value.
    Returns event_id.
    """
    _require(kind in _ADJUSTMENT_KINDS, f"Bad adjustment kind: {kind!r}")
    _require(_is_investment_account(db, account_id),
             f"Account {account_id} is not an investment account")
    if kind in ("dividend", "interest"):
        _require(amount > 0, f"{kind} amount must be positive")
        signed = amount
    elif kind == "fee":
        _require(amount > 0, "fee amount must be supplied as positive magnitude")
        signed = -amount
    else:  # gain_loss
        _require(amount != 0, "gain_loss amount must be nonzero")
        signed = amount

    open_lots = get_open_lots(db, account_id)
    total_value = sum(float(l["current_value"]) for l in open_lots)

    event_id = _write_event(
        db, event_date=event_date, account_id=account_id,
        kind=kind, amount=signed, note=note,
    )

    # Drained account: a positive adjustment (dividend/interest, or a positive
    # gain_loss) SEEDS a fresh residual lot — e.g. a penny of interest posts to
    # an account that had been emptied. A fee/loss on an empty account has
    # nothing to reduce, so it's just recorded. Never-funded accounts still error.
    if not open_lots or total_value <= 1e-9:
        if signed > 1e-9:
            _require(_has_any_lots(db, account_id),
                     f"Cannot apply {kind} to account {account_id}: it has never "
                     f"held any money — record a contribution first")
            _create_lot(db, current_account_id=account_id, origin_date=event_date,
                        origin_amount=signed, current_value=signed,
                        origin_event_id=event_id,
                        note=f"Residual {kind} seeded a lot", source="you")
        return event_id

    # Proportionally distribute `signed` across open lots
    for lot in open_lots:
        share = signed * (float(lot["current_value"]) / total_value)
        new_value = float(lot["current_value"]) + share
        _set_lot_value(db, lot["id"], new_value)

    return event_id


def _reconcile_open_lots(db, account_id: int, target: float, seed_date: str = None):
    """Scale an account's open lots so they sum exactly to `target`
    (proportional to current value). Self-heals a stale/tanked account. If the
    account has no open value but was funded and target>0, seeds one lot."""
    lots = get_open_lots(db, account_id)
    cur = sum(float(l["current_value"]) for l in lots)
    if cur <= 1e-9:
        if target > 1e-9 and _has_any_lots(db, account_id):
            _create_lot(db, current_account_id=account_id,
                        origin_date=seed_date or _today(), origin_amount=target,
                        current_value=target, note="Re-seeded by reconcile", source="you")
        return
    resid, alloc, last = target - cur, 0.0, len(lots) - 1
    for i, lot in enumerate(lots):
        sh = (resid - alloc) if i == last else resid * (float(lot["current_value"]) / cur)
        alloc += sh
        _set_lot_value(db, lot["id"], float(lot["current_value"]) + sh)


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _account_value_target(db, account_id: int):
    """The account's correct current value = its latest-DATE snapshot + any
    non-snapshot flows recorded after it. None if the account has no snapshot."""
    ls = db.execute(
        "SELECT id, event_date, snapshot_value FROM investment_events "
        "WHERE account_id=? AND kind='snapshot' ORDER BY event_date DESC, id DESC LIMIT 1",
        (account_id,)).fetchone()
    if not ls:
        return None
    after = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM investment_events WHERE account_id=? "
        "AND kind!='snapshot' AND (event_date>? OR (event_date=? AND id>?))",
        (account_id, ls["event_date"], ls["event_date"], ls["id"])).fetchone()[0]
    return float(ls["snapshot_value"]) + float(after or 0)


def record_snapshot(db, *, account_id: int, event_date: str,
                    snapshot_value: float,
                    note: Optional[str] = None) -> int:
    """Record an absolute-value snapshot for an account on a date.

    Reconciliation: residual = snapshot_value − sum(open lot current_value).
    The residual (positive or negative) is distributed proportionally
    across open lots by their current_value, treating it as unrealized
    gain/loss accumulated since the last anchor.

    For accounts with no open lots, this errors — record a contribution
    first, or use record_adjustment with a starting lot.

    Returns event_id.
    """
    _require(snapshot_value >= 0, "Snapshot value cannot be negative")
    _require(_is_investment_account(db, account_id),
             f"Account {account_id} is not an investment account")

    open_lots = get_open_lots(db, account_id)
    total_value = sum(float(l["current_value"]) for l in open_lots)

    event_id = _write_event(
        db, event_date=event_date, account_id=account_id,
        kind="snapshot", amount=None,
        snapshot_value=snapshot_value, note=note,
    )

    # ── Only the LATEST snapshot governs current lot values ────────────────
    # If a later-dated snapshot already exists, this one is a HISTORICAL
    # backfill: it must NOT change the account's current value (that reflects
    # the most recent snapshot). It's still recorded, so TWR — which reads every
    # snapshot in date order — gets more accurate. This is what lets you add
    # old snapshots freely without tanking a current balance.
    later = db.execute(
        "SELECT 1 FROM investment_events WHERE account_id=? AND kind='snapshot' "
        "AND id != ? AND (event_date > ? OR (event_date = ? AND id > ?)) LIMIT 1",
        (account_id, event_id, event_date, event_date, event_id)).fetchone()
    if later:
        # Historical backfill. It must not drive the current value — but it DOES
        # self-heal: re-tie the open lots to the account's TRUE current value
        # (its latest-DATE snapshot + any flows after it). So if the account was
        # left stale by an earlier out-of-order edit, adding any snapshot now
        # corrects it — no repair script needed, ever.
        target = _account_value_target(db, account_id)
        if target is not None:
            _reconcile_open_lots(db, account_id, target, seed_date=event_date)
        return event_id

    # Drained account (no open lots, or they sum to ~0): a snapshot of a
    # positive value SEEDS a fresh residual lot — e.g. leftover interest of a
    # penny after the account was emptied. But only if the account was ever
    # funded; a snapshot on a never-funded account is a genuine mistake.
    if not open_lots or total_value <= 1e-9:
        if snapshot_value > 1e-9:
            _require(_has_any_lots(db, account_id),
                     f"Cannot snapshot account {account_id}: it has never held "
                     f"any money — record a contribution first")
            _create_lot(db, current_account_id=account_id, origin_date=event_date,
                        origin_amount=snapshot_value, current_value=snapshot_value,
                        origin_event_id=event_id,
                        note="Residual balance re-seeded by snapshot", source="you")
        return event_id

    residual = snapshot_value - total_value

    if abs(residual) > 1e-9:
        # ── Allocate the period's gain by DOLLAR-TIME, not just value ──────
        # The residual is gain accumulated since the LAST anchor (the account's
        # previous snapshot, or each lot's own origin if there isn't one). A
        # lot's share ∝ current_value × days it was actually invested during
        # THIS period — so older money that sat in longer earns a proportionally
        # bigger slice, instead of every dollar getting the same rate regardless
        # of when it arrived. (Snapshots only — dividends/interest are point-in-
        # time and stay value-proportional.)
        prev = db.execute("""
            SELECT event_date FROM investment_events
             WHERE account_id=? AND kind='snapshot' AND event_date < ?
             ORDER BY event_date DESC, id DESC LIMIT 1
        """, (account_id, event_date)).fetchone()
        prev_date = _pdate(prev["event_date"]) if prev else None
        snap_d = _pdate(event_date)
        weights = []
        for lot in open_lots:
            start = _pdate(lot["origin_date"])
            if prev_date and prev_date > start:
                start = prev_date            # clip to this period's start
            days = (snap_d - start).days
            weights.append(float(lot["current_value"]) * max(days, 0))
        wsum = sum(weights)
        if wsum <= 0:
            # Every open lot arrived on the snapshot date (no elapsed time) —
            # fall back to value-proportional so the gain still lands.
            weights = [float(l["current_value"]) for l in open_lots]
            wsum = sum(weights)
        allocated = 0.0
        last = len(open_lots) - 1
        for i, lot in enumerate(open_lots):
            # Give the final lot the exact remainder so Σ open-lot value ties
            # to snapshot_value to the penny (no floating-point drift).
            share = (residual - allocated) if i == last \
                else residual * (weights[i] / wsum)
            allocated += share
            _set_lot_value(db, lot["id"], float(lot["current_value"]) + share)

    return event_id


def record_closure(db, *, account_id: int, event_date: str,
                   dest_account_id: Optional[int] = None,
                   note: Optional[str] = None) -> int:
    """Close an investment account. If dest_account_id is provided,
    emits a lot_move for the entire current value of the account into
    the destination (carrying lot origins). If dest_account_id is None,
    treats the closing balance as a withdrawal back to external (e.g.
    cashed out to checking).

    Marks accounts.active = 0.

    Writes a closure event AFTER any move/withdrawal so the ledger
    reads cleanly in date order. Returns the closure event_id.
    """
    _require(_is_investment_account(db, account_id),
             f"Account {account_id} is not an investment account")

    closing_value = account_value(db, account_id)

    if closing_value > 1e-9:
        if dest_account_id is not None:
            record_lot_move(
                db, src_account_id=account_id,
                dst_account_id=dest_account_id,
                event_date=event_date,
                amount=closing_value,
                note=f"Account closure → moved to {dest_account_id}"
                     + (f"; {note}" if note else ""),
            )
        else:
            record_withdrawal(
                db, account_id=account_id,
                event_date=event_date,
                amount=closing_value,
                note=f"Account closure → cashed out"
                     + (f"; {note}" if note else ""),
            )

    closure_event_id = _write_event(
        db, event_date=event_date, account_id=account_id,
        kind="closure", amount=None, note=note,
    )

    # Retire account from dropdowns
    db.execute("UPDATE accounts SET active = 0 WHERE id = ?", (account_id,))

    return closure_event_id
