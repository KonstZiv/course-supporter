"""Test purchases: their mark, and their units taken out (BIL-TESTMARK).

Purpose:
    While a payment provider is in its test mode only marked accounts may buy
    (:func:`~.payments.checkout.purchase_closed`), yet a test card's purchase
    credits a batch of real units: it was paid, at the provider's test
    environment. Operator 2026-11-16: such a batch is marked in the database,
    and its units are taken out by one administrator's command before
    charging is switched on (``BILLING_CHARGING_ENABLED``) — until then no
    unit is spent on anything.

The mark (``billing_test_batches``, :class:`MarkOrigin`):
    * ``credit`` — the purchase was credited while the provider that signed
      its event was in its test mode (:attr:`~.payments.port.PaymentProvider.
      test_mode`): :func:`mark` in the same transaction as the credit
      (``payments/webhooks.py``), so there is never a batch without its mark
      nor a mark without its batch. In live mode nothing is marked.
    * ``backfill`` — the migration ``biltestmark_test_batches`` marked every
      Creem purchase made before the marks.

    A contract invoice (B2B) and an administrator's credit are never marked.
    One mark per batch; a mark, once written, stays (the table is
    insert-only, as the journal is).

Taking the units out (:func:`void_test_batches`):
    For each marked batch, its FREE units (:func:`.journal.available`: of a
    valid batch, neither reserved nor held for a refund or a dispute) leave
    the batch as one ``test_voided`` movement — an outflow
    (:data:`~.kinds.OUTFLOW_KINDS`): the balance falls, the statement shows
    the line ``test_voided`` with minus units. Reserved and held units are
    not touched; they are reported, and a later run takes whatever of them
    has become free. A run with nothing free changes nothing. Each batch
    something was taken from gets an audit entry ``batch.test_void`` with the
    administrator's ``--actor``: ``before`` / ``after`` are
    ``{"free_units": n}`` — the batch's free units before and after (0). An
    expired batch has no free units here: its free part burns with the
    expiry (``batch_expired``), as any batch's does.

    Under each account's lock (:func:`.journal.lock_account`), so no action
    spends the units in between. ``dry_run`` reads the same and writes
    nothing (no lock either: the caller may run it in a READ ONLY
    transaction).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import audit
from course_supporter.billing.journal import (
    BatchPosition,
    available,
    lock_account,
    positions,
    write,
)
from course_supporter.billing.kinds import AuditAction, MarkOrigin, MovementKind
from course_supporter.storage.orm import BillingAccount, BillingBatch, BillingTestBatch


async def mark(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    origin: MarkOrigin,
    now: datetime,
) -> BillingTestBatch:
    """Mark the batch as a test purchase; the caller's transaction commits it."""
    row = BillingTestBatch(batch_id=batch_id, marked_at=now, marked_by=origin.value)
    session.add(row)
    await session.flush()
    return row


async def free_test_units(
    session: AsyncSession, account_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """The free units (:func:`.journal.available`) of the account's marked
    batches: what :func:`void_test_batches` would take out now."""
    now = now or datetime.now(UTC)
    marked = set(await _marked_batches(session, account_id))
    if not marked:
        return 0
    free = available(await positions(session, account_id), now)
    return sum(p.free for p in free if p.batch.id in marked)


# ── Taking the units out ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VoidedBatch:
    """One marked batch: what was taken out of it, and what was left."""

    account_id: uuid.UUID
    account_kind: str
    batch_id: uuid.UUID
    voided: int
    """Free units taken out now (0: none were free)."""
    reserved: int
    """Units left reserved for actions under way."""
    held: int
    """Units left held for a refund or a dispute."""


@dataclass(frozen=True, slots=True)
class VoidRun:
    """What one run did (or, dry, would do), batch by batch."""

    batches: list[VoidedBatch]
    dry_run: bool

    @property
    def voided(self) -> int:
        return sum(b.voided for b in self.batches)

    @property
    def reserved(self) -> int:
        return sum(b.reserved for b in self.batches)

    @property
    def held(self) -> int:
        return sum(b.held for b in self.batches)

    @property
    def accounts(self) -> int:
        return len({b.account_id for b in self.batches})


async def void_test_batches(
    session: AsyncSession,
    *,
    actor: str,
    account_id: uuid.UUID | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> VoidRun:
    """Take the free units of every marked batch out (module docstring);
    of one account's batches when ``account_id`` is given.

    Raises:
        BillingInvalidError: no actor (checked on a dry run too).
    """
    actor = audit.require_actor(actor)
    now = now or datetime.now(UTC)
    accounts = await _marked_accounts(session, account_id)
    done: list[VoidedBatch] = []
    for account in accounts:
        if not dry_run:
            await lock_account(session, account.id)
        done.extend(
            await _void_account(session, account, actor=actor, dry_run=dry_run, now=now)
        )
    return VoidRun(batches=done, dry_run=dry_run)


async def _void_account(
    session: AsyncSession,
    account: BillingAccount,
    *,
    actor: str,
    dry_run: bool,
    now: datetime,
) -> list[VoidedBatch]:
    found = await positions(session, account.id)
    by_id = {p.batch.id: p for p in found}
    free = {p.batch.id: p.free for p in available(found, now)}
    done = []
    for batch_id in await _marked_batches(session, account.id):
        position: BatchPosition | None = by_id.get(batch_id)
        voided = free.get(batch_id, 0)
        if voided and not dry_run:
            write(
                session,
                account_id=account.id,
                kind=MovementKind.TEST_VOIDED,
                units=voided,
                now=now,
                batch_id=batch_id,
                actor=actor,
            )
            await audit.record(
                session,
                actor=actor,
                action=AuditAction.BATCH_TEST_VOID,
                entity="batch",
                entity_id=batch_id,
                before={"free_units": voided},
                after={"free_units": 0},
            )
        done.append(
            VoidedBatch(
                account_id=account.id,
                account_kind=account.kind,
                batch_id=batch_id,
                voided=voided,
                reserved=position.reserved if position is not None else 0,
                held=position.held if position is not None else 0,
            )
        )
    await session.flush()
    return done


# ── Reading the marks ──────────────────────────────────────────────────


async def _marked_accounts(
    session: AsyncSession, account_id: uuid.UUID | None
) -> Sequence[BillingAccount]:
    """The accounts with a marked batch, by id."""
    query = (
        select(BillingAccount)
        .where(
            BillingAccount.id.in_(
                select(BillingBatch.account_id).join(
                    BillingTestBatch, BillingTestBatch.batch_id == BillingBatch.id
                )
            )
        )
        .order_by(BillingAccount.id)
    )
    if account_id is not None:
        query = query.where(BillingAccount.id == account_id)
    return list((await session.execute(query)).scalars())


async def _marked_batches(
    session: AsyncSession, account_id: uuid.UUID
) -> list[uuid.UUID]:
    """The account's marked batches, the oldest first."""
    rows = await session.execute(
        select(BillingBatch.id)
        .join(BillingTestBatch, BillingTestBatch.batch_id == BillingBatch.id)
        .where(BillingBatch.account_id == account_id)
        .order_by(BillingBatch.created_at, BillingBatch.id)
    )
    return list(rows.scalars())


async def marked_count(session: AsyncSession, account_id: uuid.UUID) -> int:
    """How many of the account's batches are marked as test purchases."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(BillingTestBatch)
            .join(BillingBatch, BillingBatch.id == BillingTestBatch.batch_id)
            .where(BillingBatch.account_id == account_id)
        )
        or 0
    )
