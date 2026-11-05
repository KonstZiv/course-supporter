"""Refunds of unused units, through Paddle (BIL-B, model 11).

Purpose:
    The ledger side of a refund; the Paddle adapter (week 5) will call it when
    a refund is requested and when Paddle decides. Nothing calls it yet.

The rule:
    * Per purchase batch, proportionally:
      ``floor(amount paid with tax x units refunded ÷ units of the batch)``,
      in minor units (cents) — :func:`refund_amount`.
    * Only FREE units of a VALID batch — not reserved for an action, not
      already held for another refund; an expired batch has nothing to refund.
    * An administrator's credit is never refunded.
    * While Paddle reviews the refund the units are held (:func:`refund_hold`):
      not spendable, still in the balance. Approved (:func:`refund_approve`),
      they leave the batch; rejected (:func:`refund_reject`), they are free
      again — and, like any units that become free, repay a difference first
      (operator 2026-11-05).

Idempotency:
    One refund per Paddle adjustment id: a repeated hold returns the refund
    there is; a repeated decision changes nothing; the opposite decision is
    refused.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing.errors import (
    BillingConflictError,
    BillingInvalidError,
    BillingNotFoundError,
)
from course_supporter.billing.journal import (
    lock_account,
    positions,
    repay_differences,
    write,
)
from course_supporter.billing.kinds import BatchSource, MovementKind, RefundState
from course_supporter.storage.orm import BillingBatch, BillingRefund


def refund_amount(batch: BillingBatch, units: int) -> int:
    """Money back for ``units`` of a purchase batch, in minor units, rounded down.

    Raises:
        BillingInvalidError: an administrator's credit (never refunded), or not
            between 1 and the batch's units.
    """
    if batch.source != BatchSource.PURCHASE.value or batch.amount_paid_minor is None:
        raise BillingInvalidError(
            f"batch {batch.id} is an administrator's credit: it is never refunded"
        )
    if not 0 < units <= batch.units:
        raise BillingInvalidError(
            f"a refund of batch {batch.id} is 1 to {batch.units} units, not {units}"
        )
    return batch.amount_paid_minor * units // batch.units


async def _refund(
    session: AsyncSession, paddle_adjustment_id: str, *, lock: bool = False
) -> BillingRefund | None:
    stmt = select(BillingRefund).where(
        BillingRefund.paddle_adjustment_id == paddle_adjustment_id
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def refund_hold(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    batch_id: uuid.UUID,
    units: int,
    paddle_adjustment_id: str,
    now: datetime | None = None,
) -> BillingRefund:
    """Hold free units of a purchase batch while Paddle reviews their refund.

    Raises:
        BillingNotFoundError: no such account or batch on it.
        BillingInvalidError: an administrator's credit; a number of units out
            of range (:func:`refund_amount`).
        BillingConflictError: more units than the batch has free (reserved or
            held units are not refundable; an expired batch has none); the
            adjustment already held with other figures.
    """
    now = now or datetime.now(UTC)
    await lock_account(session, account_id)
    existing = await _refund(session, paddle_adjustment_id, lock=True)
    if existing is not None:
        if (existing.account_id, existing.batch_id, existing.units) != (
            account_id,
            batch_id,
            units,
        ):
            raise BillingConflictError(
                f"Paddle adjustment {paddle_adjustment_id} is already refund "
                f"{existing.id} with other figures"
            )
        return existing
    position = next(
        (p for p in await positions(session, account_id) if p.batch.id == batch_id),
        None,
    )
    batch = position.batch if position else await session.get(BillingBatch, batch_id)
    if batch is None or batch.account_id != account_id:
        raise BillingNotFoundError(
            f"batch {batch_id} not found on account {account_id}"
        )
    amount = refund_amount(batch, units)
    free = position.free if position is not None and position.valid(now) else 0
    if units > free:
        raise BillingConflictError(
            f"batch {batch_id} has {free} free units; only free units of a valid "
            "batch are refunded (not reserved, not held)"
        )
    assert batch.currency is not None  # a purchase has a currency (check constraint)
    refund = BillingRefund(
        account_id=account_id,
        batch_id=batch_id,
        units=units,
        amount_minor=amount,
        currency=batch.currency,
        paddle_adjustment_id=paddle_adjustment_id,
        state=RefundState.HELD.value,
        created_at=now,
    )
    session.add(refund)
    await session.flush()
    write(
        session,
        account_id=account_id,
        kind=MovementKind.REFUND_HOLD,
        units=units,
        now=now,
        batch_id=batch_id,
        refund_id=refund.id,
    )
    await session.flush()
    return refund


async def _decide(
    session: AsyncSession,
    paddle_adjustment_id: str,
    state: RefundState,
    movement: MovementKind,
    now: datetime | None,
) -> BillingRefund:
    now = now or datetime.now(UTC)
    found = await _refund(session, paddle_adjustment_id)
    if found is None:
        raise BillingNotFoundError(f"no refund for adjustment {paddle_adjustment_id}")
    await lock_account(session, found.account_id)
    refund = await _refund(session, paddle_adjustment_id, lock=True)
    assert refund is not None  # a refund is never deleted
    if refund.state == state.value:
        return refund
    if refund.state != RefundState.HELD.value:
        raise BillingConflictError(
            f"refund {refund.id} was already {refund.state}, it cannot be {state.value}"
        )
    write(
        session,
        account_id=refund.account_id,
        kind=movement,
        units=refund.units,
        now=now,
        batch_id=refund.batch_id,
        refund_id=refund.id,
    )
    await session.execute(
        update(BillingRefund)
        .where(BillingRefund.id == refund.id)
        .values(state=state.value, decided_at=now)
    )
    await session.refresh(refund)
    return refund


async def refund_approve(
    session: AsyncSession, *, paddle_adjustment_id: str, now: datetime | None = None
) -> BillingRefund:
    """Paddle approved: the held units leave the batch.

    Raises:
        BillingNotFoundError: no refund for the adjustment.
        BillingConflictError: it was rejected.
    """
    return await _decide(
        session,
        paddle_adjustment_id,
        RefundState.APPROVED,
        MovementKind.REFUND_COMPLETED,
        now,
    )


async def refund_reject(
    session: AsyncSession, *, paddle_adjustment_id: str, now: datetime | None = None
) -> BillingRefund:
    """Paddle rejected: the held units are free again and repay a difference first.

    Raises:
        BillingNotFoundError: no refund for the adjustment.
        BillingConflictError: it was approved.
    """
    now = now or datetime.now(UTC)
    refund = await _decide(
        session,
        paddle_adjustment_id,
        RefundState.REJECTED,
        MovementKind.REFUND_REJECTED,
        now,
    )
    await repay_differences(session, refund.account_id, now=now)
    return refund
