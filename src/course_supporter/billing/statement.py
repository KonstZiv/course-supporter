"""What a holder sees of their units: the balance and the statement (BIL-PAY).

Purpose:
    The read side of the journal for the screens of the author and the
    student — units only, never money and never a provider's id. Nothing is
    written; the journal's functions are read, never changed.

The balance (:func:`holder_balance`):
    :func:`.ledger.balance` — balance, reserved, held, available, difference
    — as it is; for a student the difference is reported as 0 (the screens
    show a difference to authors only). ``next_expiry``: the units that burn
    first and the day they do — the free units (:func:`.journal.available`)
    of the valid batches that expire on the earliest such day (UTC). Reserved
    units are not in it: they serve their action (the journal's model 7).

The statement (:func:`statement_page`):
    The account's movements, the newest first, each with its kind for the
    screen (:class:`StatementLineKind`), its units signed — ``+`` came in, ``-`` went
    out or was blocked — and the balance after it. Reserving and releasing a
    reserve are not lines: the current reserve is in the balance.

    Which movement becomes which line is ONE table, :data:`LINES`: every
    :class:`~.kinds.MovementKind` is either there (by itself, or by the
    source of its batch / the kind of its refund) or in :data:`HIDDEN`. A
    test locks it, so a new kind of movement turns the test red instead of
    vanishing from the statement; a movement the table does not know raises
    :class:`UnmappedMovementError` rather than being dropped.

    ``balance_after`` is the balance of the journal's model 1 after the line:
    the units credited minus the units taken out of batches
    (:data:`.kinds.OUTFLOW_KINDS`), summed in the journal's order (time, then
    id). Lines that move no batch's units — a hold, a difference, units a
    benefit covered — leave it as it was. After an expiry is burned it agrees
    with :func:`.ledger.balance`.

    Pagination: ``limit`` lines (100 at most) and an opaque cursor naming the
    last line given; the order is stable. ``since`` / ``until`` bound the
    lines to ``[since, until)``.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import case, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import ledger
from course_supporter.billing.journal import available, positions
from course_supporter.billing.kinds import (
    OUTFLOW_KINDS,
    AccountKind,
    BatchSource,
    MovementKind,
    RefundKind,
)
from course_supporter.storage.orm import (
    BillingAccount,
    BillingBatch,
    BillingMovement,
    BillingRefund,
)

MAX_LIMIT: Final = 100
"""The most lines one page holds."""

# ── The balance ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NextExpiry:
    """The units that burn first, and the day (UTC) they do."""

    units: int
    expires_on: date


@dataclass(frozen=True, slots=True)
class HolderBalance:
    """Where the holder's units stand (module docstring)."""

    balance: int
    reserved: int
    held: int
    available: int
    difference: int
    next_expiry: NextExpiry | None


async def holder_balance(
    session: AsyncSession, account: BillingAccount, *, now: datetime
) -> HolderBalance:
    """The account's balance for its holder's screen (module docstring)."""
    standing = await ledger.balance(session, account.id, now=now)
    free = available(await positions(session, account.id), now)
    next_expiry = None
    if free:
        day = free[0].batch.expires_at.date()
        next_expiry = NextExpiry(
            units=sum(p.free for p in free if p.batch.expires_at.date() == day),
            expires_on=day,
        )
    return HolderBalance(
        balance=standing.balance,
        reserved=standing.reserved,
        held=standing.held,
        available=standing.available,
        difference=(
            standing.difference if account.kind == AccountKind.AUTHOR.value else 0
        ),
        next_expiry=next_expiry,
    )


# ── The statement: which movement is which line ───────────────────────


class StatementLineKind(StrEnum):
    """A statement line's kind: the key the screen translates."""

    PURCHASE = "purchase"
    ADMIN_CREDIT = "admin_credit"
    CHARGE = "charge"
    BENEFIT_COVERED = "benefit_covered"
    PLATFORM_COVERED = "platform_covered"
    EXPIRED = "expired"
    REFUND = "refund"
    REFUND_HOLD = "refund_hold"
    REFUND_RELEASED = "refund_released"
    DISPUTE_HOLD = "dispute_hold"
    DISPUTE_RELEASED = "dispute_released"
    DIFFERENCE_ARISEN = "difference_arisen"
    DIFFERENCE_REPAID = "difference_repaid"
    DIFFERENCE_ANNULLED = "difference_annulled"


@dataclass(frozen=True, slots=True)
class Line:
    """How a movement shows: its kind, and the sign of its units."""

    kind: StatementLineKind
    sign: int
    """+1: units came in (or were freed); -1: went out (or were blocked)."""


Qualifier = BatchSource | RefundKind | None
"""What else decides the line: the source of a credit's batch, the kind of a
refund's row; None for every other movement."""

LINES: Final[Mapping[tuple[MovementKind, Qualifier], Line]] = {
    (MovementKind.CREDIT, BatchSource.PURCHASE): Line(StatementLineKind.PURCHASE, +1),
    (MovementKind.CREDIT, BatchSource.ADMIN_CREDIT): Line(
        StatementLineKind.ADMIN_CREDIT, +1
    ),
    (MovementKind.DEBIT, None): Line(StatementLineKind.CHARGE, -1),
    (MovementKind.BENEFIT_COVERED, None): Line(StatementLineKind.BENEFIT_COVERED, -1),
    (MovementKind.PLATFORM_COVERED, None): Line(StatementLineKind.PLATFORM_COVERED, -1),
    (MovementKind.BATCH_EXPIRED, None): Line(StatementLineKind.EXPIRED, -1),
    (MovementKind.REFUND_HOLD, RefundKind.REFUND): Line(
        StatementLineKind.REFUND_HOLD, -1
    ),
    (MovementKind.REFUND_HOLD, RefundKind.DISPUTE): Line(
        StatementLineKind.DISPUTE_HOLD, -1
    ),
    (MovementKind.REFUND_COMPLETED, RefundKind.REFUND): Line(
        StatementLineKind.REFUND, -1
    ),
    # A dispute lost: the held units leave the batch — the money went back
    # to the payer through their bank, a refund to the screen.
    (MovementKind.REFUND_COMPLETED, RefundKind.DISPUTE): Line(
        StatementLineKind.REFUND, -1
    ),
    (MovementKind.REFUND_REJECTED, RefundKind.REFUND): Line(
        StatementLineKind.REFUND_RELEASED, +1
    ),
    (MovementKind.REFUND_REJECTED, RefundKind.DISPUTE): Line(
        StatementLineKind.DISPUTE_RELEASED, +1
    ),
    (MovementKind.DIFFERENCE_ARISEN, None): Line(
        StatementLineKind.DIFFERENCE_ARISEN, -1
    ),
    (MovementKind.DIFFERENCE_REPAID, None): Line(
        StatementLineKind.DIFFERENCE_REPAID, -1
    ),
    (MovementKind.DIFFERENCE_ANNULLED, None): Line(
        StatementLineKind.DIFFERENCE_ANNULLED, +1
    ),
}
"""Every movement the statement shows, as which line (module docstring)."""

HIDDEN: Final = frozenset({MovementKind.RESERVE, MovementKind.RESERVE_RELEASE})
"""The movements the statement does not show: the current reserve is in the
balance."""

QUALIFIED_BY_BATCH: Final = frozenset({MovementKind.CREDIT})
"""Movements whose line depends on the source of their batch."""

QUALIFIED_BY_REFUND: Final = frozenset(
    {
        MovementKind.REFUND_HOLD,
        MovementKind.REFUND_COMPLETED,
        MovementKind.REFUND_REJECTED,
    }
)
"""Movements whose line depends on the kind of their refund (or dispute)."""


class UnmappedMovementError(LookupError):
    """A movement :data:`LINES` does not know: never dropped silently."""


def line_of(kind: str, source: str | None, refund_kind: str | None) -> Line:
    """The line of a movement of ``kind``, given its batch's source and its
    refund's kind (either None when it has none).

    Raises:
        UnmappedMovementError: the table does not know it.
    """
    movement = MovementKind(kind)
    qualifier: Qualifier = None
    if movement in QUALIFIED_BY_BATCH and source is not None:
        qualifier = BatchSource(source)
    elif movement in QUALIFIED_BY_REFUND and refund_kind is not None:
        qualifier = RefundKind(refund_kind)
    found = LINES.get((movement, qualifier))
    if found is None:
        msg = f"no statement line for {movement.value} ({qualifier})"
        raise UnmappedMovementError(msg)
    return found


# ── The statement: the page ────────────────────────────────────────────


class InvalidCursorError(ValueError):
    """A cursor this module did not give."""


def _encode(at: datetime, movement_id: uuid.UUID) -> str:
    raw = json.dumps([at.isoformat(), str(movement_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at, movement_id = json.loads(raw)
        when = datetime.fromisoformat(at)
        if when.utcoffset() is None:
            raise ValueError("no zone")
        return when, uuid.UUID(movement_id)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise InvalidCursorError("not a cursor of the statement") from exc


@dataclass(frozen=True, slots=True)
class StatementLine:
    """One line of the statement (module docstring)."""

    at: datetime
    kind: StatementLineKind
    units: int
    """Signed: + came in, - went out."""
    balance_after: int
    action_kind: str | None
    action_id: str | None
    """The action the units moved for (its kind and key), when there is one."""


@dataclass(frozen=True, slots=True)
class StatementPage:
    lines: list[StatementLine]
    next_cursor: str | None
    """The cursor of the next page; None on the last."""


async def statement_page(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> StatementPage:
    """One page of the account's statement, the newest first (module docstring).

    Raises:
        InvalidCursorError: ``cursor`` is not one this function gave.
        UnmappedMovementError: a movement :data:`LINES` does not know.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    effect = case(
        (BillingMovement.kind == MovementKind.CREDIT.value, BillingMovement.units),
        (
            BillingMovement.kind.in_([k.value for k in OUTFLOW_KINDS]),
            -BillingMovement.units,
        ),
        else_=0,
    )
    journal = (
        select(
            BillingMovement.id,
            BillingMovement.created_at,
            BillingMovement.kind,
            BillingMovement.units,
            BillingMovement.batch_id,
            BillingMovement.refund_id,
            BillingMovement.action_kind,
            BillingMovement.action_key,
            func.sum(effect)
            .over(
                order_by=(BillingMovement.created_at, BillingMovement.id),
                rows=(None, 0),
            )
            .label("balance_after"),
        )
        .where(BillingMovement.account_id == account_id)
        .subquery()
    )
    stmt = (
        select(journal, BillingBatch.source, BillingRefund.kind.label("refund_kind"))
        .outerjoin(BillingBatch, BillingBatch.id == journal.c.batch_id)
        .outerjoin(BillingRefund, BillingRefund.id == journal.c.refund_id)
        .where(journal.c.kind.not_in([k.value for k in HIDDEN]))
    )
    if since is not None:
        stmt = stmt.where(journal.c.created_at >= since)
    if until is not None:
        stmt = stmt.where(journal.c.created_at < until)
    if cursor is not None:
        at, last = _decode(cursor)
        stmt = stmt.where(tuple_(journal.c.created_at, journal.c.id) < (at, last))
    rows = (
        await session.execute(
            stmt.order_by(journal.c.created_at.desc(), journal.c.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    lines = []
    for row in rows[:limit]:
        line = line_of(row.kind, row.source, row.refund_kind)
        lines.append(
            StatementLine(
                at=row.created_at,
                kind=line.kind,
                units=line.sign * row.units,
                balance_after=int(row.balance_after),
                action_kind=row.action_kind,
                action_id=row.action_key,
            )
        )
    more = len(rows) > limit
    last_row = rows[limit - 1] if more else None
    return StatementPage(
        lines=lines,
        next_cursor=(
            _encode(last_row.created_at, last_row.id) if last_row is not None else None
        ),
    )
