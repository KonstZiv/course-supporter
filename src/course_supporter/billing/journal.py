"""The arithmetic of the journal of units (BIL-B): what each batch holds.

Purpose:
    The shared ground of :mod:`.ledger`, :mod:`.expiry` and :mod:`.refunds`:
    the lock of an account, the position of each of its batches, one movement
    written, and the repayment of differences. Nothing here is called from
    outside billing.

A batch's position (nothing of it is stored — the journal is the truth):

    remaining = units - Σ movements that take units out of the batch
                (:data:`.kinds.OUTFLOW_KINDS`)
    reserved  = Σ its parts in ACTIVE reservations (their ``allocation``)
    held      = Σ its units in HELD refunds
    free      = remaining - reserved - held

    A batch is VALID while ``now < expires_at``. Only the free units of valid
    batches are available — to a new reserve, to a charge beyond a reserve,
    to a difference. The reserved part of an expired batch still serves its
    action (model 7); its free part waits for :func:`.expiry.expire_batches`.

Order:
    Units are always taken from the batch that expires first (then the older
    batch, then the lower id), whatever takes them.

Locking:
    Every change of an account's units starts with :func:`lock_account`
    (``SELECT … FOR UPDATE`` on its ``billing_accounts`` row) and reads the
    positions after it, so two actions never spend the same units. The lock
    lasts until the caller's transaction ends.
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing.errors import BillingConflictError, BillingNotFoundError
from course_supporter.billing.kinds import (
    OUTFLOW_KINDS,
    AccountStatus,
    ActionKind,
    DifferenceState,
    MovementKind,
    PayerSource,
    RefundState,
    ReservationState,
)
from course_supporter.storage.orm import (
    BillingAccount,
    BillingBatch,
    BillingDifference,
    BillingMovement,
    BillingRefund,
    BillingReservation,
)

VALIDITY_MONTHS: Final = 6
"""A batch is valid, and a difference stands, six calendar months."""


def add_months(at: datetime, months: int) -> datetime:
    """``at`` plus calendar months; a day the month lacks becomes its last.

    31 August + 6 months = 28 (29) February — the rule of Postgres's
    ``+ interval 'n months'``. The time of day and the zone stay.
    """
    total = at.month - 1 + months
    year, month = at.year + total // 12, total % 12 + 1
    day = min(at.day, calendar.monthrange(year, month)[1])
    return at.replace(year=year, month=month, day=day)


@dataclass(frozen=True, slots=True)
class Action:
    """The action units move for: its kind and key (as ``action_cost_estimates``)."""

    kind: ActionKind
    key: str


@dataclass(frozen=True, slots=True)
class Payment:
    """Whom an action was for and why this account paid; on charge movements."""

    beneficiary_account_id: uuid.UUID
    payer_source: PayerSource
    rule_id: uuid.UUID | None


@dataclass(slots=True)
class BatchPosition:
    """One batch and what of it is left, held and free (module docstring)."""

    batch: BillingBatch
    remaining: int
    reserved: int
    held: int

    @property
    def free(self) -> int:
        """Units neither reserved nor held for a refund."""
        return self.remaining - self.reserved - self.held

    def valid(self, now: datetime) -> bool:
        """True until the batch expires."""
        return now < self.batch.expires_at


async def lock_account(
    session: AsyncSession, account_id: uuid.UUID, *, active: bool = False
) -> BillingAccount:
    """Lock the account's row until the transaction ends; return the account.

    Raises:
        BillingNotFoundError: no such account.
        BillingConflictError: ``active`` is asked and the account is closed
            (nothing new arrives on, or is reserved from, a closed account).
    """
    account = (
        await session.execute(
            select(BillingAccount)
            .where(BillingAccount.id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if account is None:
        raise BillingNotFoundError(f"account {account_id} not found")
    if active and account.status != AccountStatus.ACTIVE.value:
        raise BillingConflictError(f"account {account_id} is closed")
    return account


def allocation_parts(reservation: BillingReservation) -> list[tuple[uuid.UUID, int]]:
    """The reservation's ``allocation`` as (batch id, units), in its order."""
    return [
        (uuid.UUID(str(part["batch_id"])), int(part["units"]))
        for part in reservation.allocation
    ]


async def positions(
    session: AsyncSession, account_id: uuid.UUID
) -> list[BatchPosition]:
    """Every batch of the account with units left, earliest expiry first."""
    outflow = (
        select(
            BillingMovement.batch_id,
            func.sum(BillingMovement.units).label("units"),
        )
        .where(
            BillingMovement.account_id == account_id,
            BillingMovement.kind.in_([k.value for k in OUTFLOW_KINDS]),
        )
        .group_by(BillingMovement.batch_id)
        .subquery()
    )
    taken = func.coalesce(outflow.c.units, 0)
    rows = await session.execute(
        select(BillingBatch, taken)
        .outerjoin(outflow, outflow.c.batch_id == BillingBatch.id)
        .where(BillingBatch.account_id == account_id, BillingBatch.units > taken)
        .order_by(BillingBatch.expires_at, BillingBatch.created_at, BillingBatch.id)
    )
    found = [
        BatchPosition(batch=batch, remaining=batch.units - int(out), reserved=0, held=0)
        for batch, out in rows.tuples()
    ]
    by_id = {p.batch.id: p for p in found}

    reservations = await session.execute(
        select(BillingReservation).where(
            BillingReservation.account_id == account_id,
            BillingReservation.state == ReservationState.ACTIVE.value,
        )
    )
    for reservation in reservations.scalars():
        for batch_id, units in allocation_parts(reservation):
            by_id[batch_id].reserved += units

    held = await session.execute(
        select(BillingRefund.batch_id, func.sum(BillingRefund.units))
        .where(
            BillingRefund.account_id == account_id,
            BillingRefund.state == RefundState.HELD.value,
        )
        .group_by(BillingRefund.batch_id)
    )
    for batch_id, units in held.tuples():
        by_id[batch_id].held += int(units)
    return found


def available(found: Sequence[BatchPosition], now: datetime) -> list[BatchPosition]:
    """The valid batches with free units, in the order units are taken."""
    return [p for p in found if p.valid(now) and p.free > 0]


def write(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    kind: MovementKind,
    units: int,
    now: datetime,
    batch_id: uuid.UUID | None = None,
    difference_id: uuid.UUID | None = None,
    refund_id: uuid.UUID | None = None,
    action: Action | None = None,
    payment: Payment | None = None,
    actor: str | None = None,
) -> BillingMovement:
    """Add one movement to the session (flushed by the caller's next flush)."""
    movement = BillingMovement(
        account_id=account_id,
        kind=kind.value,
        units=units,
        batch_id=batch_id,
        difference_id=difference_id,
        refund_id=refund_id,
        action_kind=action.kind.value if action is not None else None,
        action_key=action.key if action is not None else None,
        beneficiary_account_id=(
            payment.beneficiary_account_id if payment is not None else None
        ),
        payer_source=payment.payer_source.value if payment is not None else None,
        rule_id=payment.rule_id if payment is not None else None,
        actor=actor,
        created_at=now,
    )
    session.add(movement)
    return movement


@dataclass(frozen=True, slots=True)
class Repayment:
    """Units of one batch that repaid one difference."""

    difference_id: uuid.UUID
    batch_id: uuid.UUID
    units: int


async def repay_differences(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    now: datetime,
    action: Action | None = None,
) -> list[Repayment]:
    """Repay the account's open differences from its available units.

    Called under the account's lock after anything that makes units free —
    a batch credited, a reserve released (the rest after a settle, or a
    failure), a refund rejected. The oldest difference first; units from the
    batch that expires first. Since this runs after every such event, a
    difference and available units never coexist: what is available here is
    exactly what the event freed. Units of an EXPIRED batch are not available
    and never repay — they burn (:func:`.expiry.expire_batches`).

    ``action`` names the action whose released units repay, for the journal.
    """
    open_differences = list(
        (
            await session.execute(
                select(BillingDifference)
                .where(
                    BillingDifference.account_id == account_id,
                    BillingDifference.state == DifferenceState.OPEN.value,
                )
                .order_by(BillingDifference.created_at, BillingDifference.id)
            )
        ).scalars()
    )
    if not open_differences:
        return []
    sources = available(await positions(session, account_id), now)
    done: list[Repayment] = []
    for difference in open_differences:
        outstanding = difference.outstanding_units
        for position in sources:
            if outstanding == 0:
                break
            take = min(position.free, outstanding)
            if take == 0:
                continue
            write(
                session,
                account_id=account_id,
                kind=MovementKind.DIFFERENCE_REPAID,
                units=take,
                now=now,
                batch_id=position.batch.id,
                difference_id=difference.id,
                action=action,
            )
            position.remaining -= take
            outstanding -= take
            done.append(Repayment(difference.id, position.batch.id, take))
        if outstanding == difference.outstanding_units:
            break  # nothing left to repay with
        await session.execute(
            update(BillingDifference)
            .where(BillingDifference.id == difference.id)
            .values(
                outstanding_units=outstanding,
                state=(
                    DifferenceState.OPEN.value
                    if outstanding
                    else DifferenceState.REPAID.value
                ),
                closed_at=None if outstanding else now,
            )
        )
        if outstanding:
            break  # the available units are spent
    await session.flush()
    return done
