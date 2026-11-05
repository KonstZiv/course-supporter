"""Expiry of batches and annulment of differences (BIL-B): for the scheduler.

Purpose:
    Two functions the scheduler of BIL-B part 2 will run; nothing runs them
    yet. Each is idempotent — a second run at the same moment finds nothing —
    and safe to run at any time: it locks each account it changes
    (:func:`.journal.lock_account`), like every other change of units.

Batches (model 7):
    A batch is valid six calendar months. At its expiry its FREE units burn
    (a ``batch_expired`` movement). Units of it still reserved for an action
    wait for that action: the action is charged from them as from any reserve
    (:func:`.ledger.settle`); what the action leaves is released and burns at
    the next run. Units held for a refund wait for Paddle's decision the same
    way. Released units of an expired batch never repay a difference
    (operator 2026-11-05): they are not available.

Differences (model 8):
    An open difference is annulled once ``annul_after`` has passed (six
    months after it arose; never for an author account allowed credit when it
    arose — ``annul_after`` is NULL then): a ``difference_annulled`` movement
    of what was left, and the difference closes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing.journal import lock_account, positions, write
from course_supporter.billing.kinds import (
    OUTFLOW_KINDS,
    DifferenceState,
    MovementKind,
)
from course_supporter.storage.orm import (
    BillingBatch,
    BillingDifference,
    BillingMovement,
)


@dataclass(frozen=True, slots=True)
class Burned:
    """Free units of an expired batch that burned."""

    account_id: uuid.UUID
    batch_id: uuid.UUID
    units: int


@dataclass(frozen=True, slots=True)
class Annulled:
    """What was left of a difference, annulled."""

    account_id: uuid.UUID
    difference_id: uuid.UUID
    units: int


async def _accounts_with_expired_units(
    session: AsyncSession, now: datetime
) -> list[uuid.UUID]:
    taken = (
        select(func.coalesce(func.sum(BillingMovement.units), 0))
        .where(
            BillingMovement.batch_id == BillingBatch.id,
            BillingMovement.kind.in_([k.value for k in OUTFLOW_KINDS]),
        )
        .scalar_subquery()
    )
    rows = await session.execute(
        select(BillingBatch.account_id)
        .where(BillingBatch.expires_at <= now, BillingBatch.units > taken)
        .distinct()
        .order_by(BillingBatch.account_id)
    )
    return list(rows.scalars())


async def expire_batches(session: AsyncSession, now: datetime) -> list[Burned]:
    """Burn the free units of every batch expired by ``now`` (module docstring)."""
    burned: list[Burned] = []
    for account_id in await _accounts_with_expired_units(session, now):
        await lock_account(session, account_id)
        for position in await positions(session, account_id):
            if position.valid(now) or position.free <= 0:
                continue
            write(
                session,
                account_id=account_id,
                kind=MovementKind.BATCH_EXPIRED,
                units=position.free,
                now=now,
                batch_id=position.batch.id,
            )
            burned.append(Burned(account_id, position.batch.id, position.free))
    await session.flush()
    return burned


async def annul_differences(session: AsyncSession, now: datetime) -> list[Annulled]:
    """Annul what is left of every open difference whose term passed by ``now``."""
    due = (
        select(BillingDifference)
        .where(
            BillingDifference.state == DifferenceState.OPEN.value,
            BillingDifference.annul_after.is_not(None),
            BillingDifference.annul_after <= now,
        )
        .order_by(
            BillingDifference.account_id,
            BillingDifference.created_at,
            BillingDifference.id,
        )
    )
    account_ids = sorted({d.account_id for d in (await session.execute(due)).scalars()})
    annulled: list[Annulled] = []
    for account_id in account_ids:
        await lock_account(session, account_id)
        rows = await session.execute(
            due.where(BillingDifference.account_id == account_id).execution_options(
                populate_existing=True
            )
        )
        for difference in rows.scalars():
            write(
                session,
                account_id=account_id,
                kind=MovementKind.DIFFERENCE_ANNULLED,
                units=difference.outstanding_units,
                now=now,
                difference_id=difference.id,
            )
            annulled.append(
                Annulled(account_id, difference.id, difference.outstanding_units)
            )
            await session.execute(
                update(BillingDifference)
                .where(BillingDifference.id == difference.id)
                .values(
                    state=DifferenceState.ANNULLED.value,
                    outstanding_units=0,
                    closed_at=now,
                )
            )
    await session.flush()
    return annulled
