"""Refunds of unused units, through the batch's payment provider (BIL-B, model 11),
and the units of a disputed payment (BIL-PAY).

Purpose:
    The ledger side of a refund. The provider's webhook
    (``billing.payments.webhooks``) calls :func:`refund_approve` /
    :func:`refund_reject` when the provider decides; what asks the provider
    for a refund and holds its units (:func:`refund_hold`) is not built yet.
    A refund the journal never held — made at the provider, in its dashboard
    — is followed by :func:`refund_at_provider`; a dispute (chargeback) holds
    the batch's free units (:func:`dispute_hold`) until it is lost
    (:func:`dispute_lost`) or won (:func:`dispute_won`).

The rule:
    * Per purchase batch, proportionally:
      ``floor(amount paid with tax x units refunded ÷ units of the batch)``,
      in minor units (cents) — :func:`refund_amount`.
    * Only FREE units of a VALID batch — not reserved for an action, not
      already held for another refund; an expired batch has nothing to refund.
    * An administrator's credit is never refunded.
    * While the provider reviews the refund the units are held
      (:func:`refund_hold`): not spendable, still in the balance. Approved
      (:func:`refund_approve`), they leave the batch; rejected
      (:func:`refund_reject`), they are free again — and, like any units that
      become free, repay a difference first (operator 2026-11-05).

A refund made at the provider (BIL-PAY):
    The provider says how much money went back; the journal takes
    ``ceil(amount refunded x units of the batch ÷ amount paid)`` units, no
    more than the batch has free — what is not free (spent, reserved, held,
    expired) is the platform's loss, no debt arises. The refund is written
    approved at once: a hold and its completion, one after the other.

A dispute (BIL-PAY, operator 2026-11-12):
    The payer's bank takes the money back while the dispute lasts. Every
    free unit of the batch is held — by the same mechanism as a refund, a
    ``billing_refunds`` row of kind ``dispute`` keyed by the dispute's id.
    Lost: the units leave the batch (``approved``). Won: they are free again
    (``rejected``) and repay a difference first. Units spent before the
    dispute are the platform's loss; no debt arises, and nothing stops new
    purchases. A provider that reports the outcome by an event calls the same
    functions; one that does not (Creem) leaves it to the administrator
    (``scripts/billing_admin.py dispute-lost`` / ``dispute-won``).

Idempotency:
    One refund (or dispute) per adjustment of a provider (``provider`` +
    ``provider_adjustment_id``): a repeated hold returns the refund there is;
    a repeated decision changes nothing; the opposite decision is refused.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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
    require_provider,
    write,
)
from course_supporter.billing.kinds import (
    BatchSource,
    MovementKind,
    RefundKind,
    RefundState,
)
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
    session: AsyncSession,
    provider: str,
    provider_adjustment_id: str,
    *,
    lock: bool = False,
) -> BillingRefund | None:
    stmt = select(BillingRefund).where(
        BillingRefund.provider == provider,
        BillingRefund.provider_adjustment_id == provider_adjustment_id,
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_refund(
    session: AsyncSession, *, provider: str, provider_adjustment_id: str
) -> BillingRefund | None:
    """The refund (or dispute) of the provider's adjustment, if there is one."""
    return await _refund(session, provider, provider_adjustment_id)


async def refund_hold(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    batch_id: uuid.UUID,
    units: int,
    provider: str,
    provider_adjustment_id: str,
    now: datetime | None = None,
) -> BillingRefund:
    """Hold free units of a purchase batch while its provider reviews their refund.

    ``provider`` is the batch's: a refund goes back the way the money came.

    Raises:
        BillingNotFoundError: no such account or batch on it.
        BillingInvalidError: an administrator's credit; a number of units out
            of range (:func:`refund_amount`); not the batch's provider.
        BillingConflictError: more units than the batch has free (reserved or
            held units are not refundable; an expired batch has none); the
            adjustment already held with other figures.
    """
    now = now or datetime.now(UTC)
    provider = require_provider(provider)
    await lock_account(session, account_id)
    existing = await _refund(session, provider, provider_adjustment_id, lock=True)
    if existing is not None:
        if (existing.account_id, existing.batch_id, existing.units, existing.kind) != (
            account_id,
            batch_id,
            units,
            RefundKind.REFUND.value,
        ):
            raise BillingConflictError(
                f"{provider} adjustment {provider_adjustment_id} is already "
                f"refund {existing.id} with other figures"
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
    if batch.provider != provider:
        raise BillingInvalidError(
            f"batch {batch_id} was paid through {batch.provider}, not {provider}"
        )
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
        kind=RefundKind.REFUND.value,
        units=units,
        amount_minor=amount,
        currency=batch.currency,
        provider=provider,
        provider_adjustment_id=provider_adjustment_id,
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
    provider: str,
    provider_adjustment_id: str,
    state: RefundState,
    movement: MovementKind,
    now: datetime | None,
    kind: RefundKind = RefundKind.REFUND,
) -> BillingRefund:
    now = now or datetime.now(UTC)
    found = await _refund(session, provider, provider_adjustment_id)
    if found is None:
        what = "dispute" if kind is RefundKind.DISPUTE else "adjustment"
        raise BillingNotFoundError(
            f"no {kind.value} for {provider} {what} {provider_adjustment_id}"
        )
    if found.kind != kind.value:
        raise BillingConflictError(
            f"{provider} {provider_adjustment_id} is a {found.kind}, not a {kind.value}"
        )
    await lock_account(session, found.account_id)
    refund = await _refund(session, provider, provider_adjustment_id, lock=True)
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
    session: AsyncSession,
    *,
    provider: str,
    provider_adjustment_id: str,
    now: datetime | None = None,
) -> BillingRefund:
    """The provider approved: the held units leave the batch.

    Raises:
        BillingNotFoundError: no refund for the provider's adjustment.
        BillingConflictError: it was rejected.
    """
    return await _decide(
        session,
        provider,
        provider_adjustment_id,
        RefundState.APPROVED,
        MovementKind.REFUND_COMPLETED,
        now,
    )


async def refund_reject(
    session: AsyncSession,
    *,
    provider: str,
    provider_adjustment_id: str,
    now: datetime | None = None,
) -> BillingRefund:
    """The provider rejected: the held units are free again and repay a
    difference first.

    Raises:
        BillingNotFoundError: no refund for the provider's adjustment.
        BillingConflictError: it was approved.
    """
    now = now or datetime.now(UTC)
    refund = await _decide(
        session,
        provider,
        provider_adjustment_id,
        RefundState.REJECTED,
        MovementKind.REFUND_REJECTED,
        now,
    )
    await repay_differences(session, refund.account_id, now=now)
    return refund


# ── Made at the provider; disputes (BIL-PAY) ──────────────────────────


async def _batch_of_transaction(
    session: AsyncSession, provider: str, transaction_id: str | None
) -> BillingBatch:
    """The one purchase batch of the provider's transaction.

    Raises:
        BillingNotFoundError: none (no id, or no such purchase).
        BillingConflictError: several — a transaction of several lines; no
            adapter makes one today, and which line is meant is a person's call.
    """
    if transaction_id is None:
        raise BillingNotFoundError(f"the {provider} event names no transaction")
    batches = list(
        (
            await session.execute(
                select(BillingBatch).where(
                    BillingBatch.provider == provider,
                    BillingBatch.provider_transaction_id == transaction_id,
                )
            )
        ).scalars()
    )
    if not batches:
        raise BillingNotFoundError(
            f"no purchase batch of {provider} transaction {transaction_id}"
        )
    if len(batches) > 1:
        raise BillingConflictError(
            f"{provider} transaction {transaction_id} has {len(batches)} batches; "
            "settle it by hand"
        )
    return batches[0]


async def _free_units(session: AsyncSession, batch: BillingBatch, now: datetime) -> int:
    """The batch's free units now (none once it expired); under the lock."""
    position = next(
        (
            p
            for p in await positions(session, batch.account_id)
            if p.batch.id == batch.id
        ),
        None,
    )
    return position.free if position is not None and position.valid(now) else 0


@dataclass(frozen=True, slots=True)
class Taken:
    """What a refund made at the provider, or a dispute, did to its batch."""

    batch_id: uuid.UUID
    account_id: uuid.UUID
    wanted: int
    """Units the money stands for (all of the batch's free units for a dispute)."""
    units: int
    """Units taken (a refund) or held (a dispute): no more than were free."""
    refund: BillingRefund | None
    """The row; None when nothing was free (no row is written then)."""
    repeated: bool = False
    """True when the adjustment (or dispute) was handled before."""

    @property
    def lost(self) -> int:
        """Units the money stood for that were no longer free: the platform's."""
        return self.wanted - self.units


def units_for_amount(batch: BillingBatch, amount_minor: int) -> int:
    """The units ``amount_minor`` of the batch's payment stands for, rounded up.

    ``ceil(amount x units ÷ amount paid)``, no more than the batch's units; a
    batch paid nothing gives all its units for any money back.

    Raises:
        BillingInvalidError: an administrator's credit, or a negative amount.
    """
    if batch.source != BatchSource.PURCHASE.value or batch.amount_paid_minor is None:
        raise BillingInvalidError(
            f"batch {batch.id} is an administrator's credit: it is never refunded"
        )
    if amount_minor < 0:
        raise BillingInvalidError("an amount refunded cannot be negative")
    if amount_minor == 0:
        return 0
    paid = batch.amount_paid_minor
    if paid == 0:
        return batch.units
    return min(batch.units, -(-amount_minor * batch.units // paid))


def _take(
    session: AsyncSession,
    *,
    refund: BillingRefund,
    now: datetime,
    complete: bool,
) -> None:
    write(
        session,
        account_id=refund.account_id,
        kind=MovementKind.REFUND_HOLD,
        units=refund.units,
        now=now,
        batch_id=refund.batch_id,
        refund_id=refund.id,
    )
    if complete:
        write(
            session,
            account_id=refund.account_id,
            kind=MovementKind.REFUND_COMPLETED,
            units=refund.units,
            now=now,
            batch_id=refund.batch_id,
            refund_id=refund.id,
        )


async def refund_at_provider(
    session: AsyncSession,
    *,
    provider: str,
    provider_adjustment_id: str,
    transaction_id: str | None,
    amount_minor: int,
    currency: str,
    now: datetime | None = None,
) -> Taken:
    """Follow a refund made at the provider, which the journal never held.

    Finds the purchase batch of the transaction and takes the units the money
    stands for (:func:`units_for_amount`), no more than are free; the refund
    is written approved at once (a hold and its completion). Nothing free:
    nothing is written, and :attr:`Taken.lost` says what the platform lost.

    Idempotent by the adjustment: a refund there is already is returned as it
    is (``repeated``).

    Raises:
        BillingNotFoundError: no transaction, or no purchase batch of it.
        BillingConflictError: several batches of the transaction; the
            adjustment is a held refund or a dispute (the webhook approves a
            held refund itself).
        BillingInvalidError: another currency than the batch's; a negative
            amount; an administrator's credit.
    """
    now = now or datetime.now(UTC)
    provider = require_provider(provider)
    batch = await _batch_of_transaction(session, provider, transaction_id)
    if currency != batch.currency:
        raise BillingInvalidError(
            f"a refund in {currency} of batch {batch.id} paid in {batch.currency}"
        )
    wanted = units_for_amount(batch, amount_minor)
    await lock_account(session, batch.account_id)
    existing = await _refund(session, provider, provider_adjustment_id, lock=True)
    if existing is not None:
        if (
            existing.kind != RefundKind.REFUND.value
            or existing.state != RefundState.APPROVED.value
            or existing.batch_id != batch.id
        ):
            raise BillingConflictError(
                f"{provider} adjustment {provider_adjustment_id} is already "
                f"{existing.kind} {existing.id}, {existing.state}"
            )
        return Taken(
            batch.id, batch.account_id, wanted, existing.units, existing, repeated=True
        )
    units = min(wanted, await _free_units(session, batch, now))
    if units == 0:
        return Taken(batch.id, batch.account_id, wanted, 0, None)
    refund = BillingRefund(
        account_id=batch.account_id,
        batch_id=batch.id,
        kind=RefundKind.REFUND.value,
        units=units,
        amount_minor=amount_minor,
        currency=currency,
        provider=provider,
        provider_adjustment_id=provider_adjustment_id,
        state=RefundState.APPROVED.value,
        created_at=now,
        decided_at=now,
    )
    session.add(refund)
    await session.flush()
    _take(session, refund=refund, now=now, complete=True)
    await session.flush()
    return Taken(batch.id, batch.account_id, wanted, units, refund)


async def dispute_hold(
    session: AsyncSession,
    *,
    provider: str,
    dispute_id: str,
    transaction_id: str | None,
    now: datetime | None = None,
) -> Taken:
    """Hold every free unit of the disputed purchase until the dispute ends.

    The row is a ``billing_refunds`` of kind ``dispute``, keyed by the
    dispute's id; its amount is the worth of the units held
    (:func:`refund_amount`) — the disputed money itself stays on the
    provider's event. Nothing free: nothing is written (the units were spent
    before the dispute: the platform's loss, no debt).

    Idempotent by the dispute: a hold there is already is returned
    (``repeated``).

    Raises:
        BillingNotFoundError: no transaction, or no purchase batch of it.
        BillingConflictError: several batches of the transaction; the id is
            a refund's.
    """
    now = now or datetime.now(UTC)
    provider = require_provider(provider)
    batch = await _batch_of_transaction(session, provider, transaction_id)
    await lock_account(session, batch.account_id)
    existing = await _refund(session, provider, dispute_id, lock=True)
    if existing is not None:
        if existing.kind != RefundKind.DISPUTE.value or existing.batch_id != batch.id:
            raise BillingConflictError(
                f"{provider} {dispute_id} is already {existing.kind} {existing.id}"
            )
        return Taken(
            batch.id,
            batch.account_id,
            existing.units,
            existing.units,
            existing,
            repeated=True,
        )
    free = await _free_units(session, batch, now)
    if free == 0:
        return Taken(batch.id, batch.account_id, 0, 0, None)
    assert batch.currency is not None  # a purchase has a currency (check constraint)
    refund = BillingRefund(
        account_id=batch.account_id,
        batch_id=batch.id,
        kind=RefundKind.DISPUTE.value,
        units=free,
        amount_minor=refund_amount(batch, free),
        currency=batch.currency,
        provider=provider,
        provider_adjustment_id=dispute_id,
        state=RefundState.HELD.value,
        created_at=now,
    )
    session.add(refund)
    await session.flush()
    _take(session, refund=refund, now=now, complete=False)
    await session.flush()
    return Taken(batch.id, batch.account_id, free, free, refund)


async def dispute_lost(
    session: AsyncSession,
    *,
    provider: str,
    dispute_id: str,
    now: datetime | None = None,
) -> BillingRefund:
    """The dispute was lost: the held units leave the batch.

    Raises:
        BillingNotFoundError: nothing was held for the dispute.
        BillingConflictError: it was won; the id is a refund's.
    """
    return await _decide(
        session,
        provider,
        dispute_id,
        RefundState.APPROVED,
        MovementKind.REFUND_COMPLETED,
        now,
        kind=RefundKind.DISPUTE,
    )


async def dispute_won(
    session: AsyncSession,
    *,
    provider: str,
    dispute_id: str,
    now: datetime | None = None,
) -> BillingRefund:
    """The dispute was won: the held units are free again and repay a
    difference first.

    Raises:
        BillingNotFoundError: nothing was held for the dispute.
        BillingConflictError: it was lost; the id is a refund's.
    """
    now = now or datetime.now(UTC)
    refund = await _decide(
        session,
        provider,
        dispute_id,
        RefundState.REJECTED,
        MovementKind.REFUND_REJECTED,
        now,
        kind=RefundKind.DISPUTE,
    )
    await repay_differences(session, refund.account_id, now=now)
    return refund
