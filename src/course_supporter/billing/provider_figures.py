"""What the payment provider says of a purchase's money (Б-3, operator 2026-11-27).

Purpose:
    The partner's report (:mod:`.partner_report`) takes the tax and the
    provider's fee out of what the students paid. The journal knows what was
    paid (``billing_batches.amount_paid_minor``, tax included); the provider
    knows the rest. Four columns of the purchase's batch keep it, in the
    batch's currency: ``provider_tax_minor``, ``provider_fee_minor``,
    ``provider_refunded_minor`` and ``provider_synced_at`` (the batch is
    otherwise insert-only; its trigger lets these four through).

Two writers, one rule (:func:`_merged`):
    * the purchase's webhook — :func:`record_from_purchase`, in the
      transaction of the credit: the figures the event carries (Creem's: the
      tax, never a fee). Not audited, as the credit itself is not;
    * :func:`sync_batches` — ``scripts/billing_admin.py creem-sync``: the paid
      batches of one provider created in a period, each asked of the
      provider's API (:class:`~.payments.port.TransactionLookup`). Each batch
      that changes is audited (``batch.provider_sync``, before/after: the four
      columns) with the administrator's actor.

    A figure the provider gives replaces the one kept; a figure it does not
    give (None) leaves the kept one — never a guess. When nothing changes
    nothing is written, ``provider_synced_at`` included: a second run changes
    nothing. Figures in another currency than the batch's are not written
    (:attr:`SyncRun.other_currency`).

A paid batch is a purchase not marked as a test one (``billing_test_batches``)
— the batches whose money the reports count (:mod:`.review_money`).

Read by the partner's report only; nothing that charges, reserves or refunds
reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import Select, select

from course_supporter.billing import audit
from course_supporter.billing.costs import utc_bounds
from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import AuditAction, BatchSource
from course_supporter.billing.payments.port import (
    PaymentProviderError,
    TransactionFigures,
    TransactionLookup,
)
from course_supporter.storage.orm import BillingBatch, BillingTestBatch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class Figures:
    """The provider's figures of a batch; None — not known."""

    tax_minor: int | None = None
    fee_minor: int | None = None
    refunded_minor: int | None = None

    @classmethod
    def of(cls, batch: BillingBatch) -> Figures:
        """The figures the batch keeps."""
        return cls(
            tax_minor=batch.provider_tax_minor,
            fee_minor=batch.provider_fee_minor,
            refunded_minor=batch.provider_refunded_minor,
        )


def _merged(kept: Figures, given: Figures) -> Figures:
    """What the provider gives replaces what is kept; None leaves it."""
    return Figures(
        tax_minor=given.tax_minor if given.tax_minor is not None else kept.tax_minor,
        fee_minor=given.fee_minor if given.fee_minor is not None else kept.fee_minor,
        refunded_minor=(
            given.refunded_minor
            if given.refunded_minor is not None
            else kept.refunded_minor
        ),
    )


def figures_snapshot(batch: BillingBatch) -> dict[str, Any]:
    """The four columns, for the audit entry of a sync."""
    synced = batch.provider_synced_at
    return {
        "provider_tax_minor": batch.provider_tax_minor,
        "provider_fee_minor": batch.provider_fee_minor,
        "provider_refunded_minor": batch.provider_refunded_minor,
        "provider_synced_at": synced.isoformat() if synced is not None else None,
    }


def _write(batch: BillingBatch, figures: Figures, now: datetime) -> bool:
    """Put the merged figures on the batch; False when nothing would change."""
    merged = _merged(Figures.of(batch), figures)
    if merged == Figures.of(batch):
        return False
    batch.provider_tax_minor = merged.tax_minor
    batch.provider_fee_minor = merged.fee_minor
    batch.provider_refunded_minor = merged.refunded_minor
    batch.provider_synced_at = now
    return True


async def record_from_purchase(
    session: AsyncSession,
    *,
    batch: BillingBatch,
    tax_minor: int | None,
    fee_minor: int | None,
    now: datetime,
) -> bool:
    """The figures a purchase's event carries, on its batch (module docstring).

    Flushes; the caller (the webhook's transaction) commits. False when the
    event carries none, or the batch has them already.
    """
    if batch.source != BatchSource.PURCHASE.value:
        return False
    changed = _write(batch, Figures(tax_minor=tax_minor, fee_minor=fee_minor), now)
    if changed:
        await session.flush()
    return changed


# ── creem-sync ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SyncRun:
    """What :func:`sync_batches` did — or, in a dry run, would do."""

    dry_run: bool
    batches: int
    """Paid batches of the provider created in the period."""
    updated: int
    """Batches whose figures changed (in a dry run: would change)."""
    unchanged: int
    not_found: int
    """Batches the provider knows no payment of."""
    other_currency: int
    """Batches the provider reports in another currency: not written."""
    failed: int
    """Batches the provider's API failed for: left as they were."""


def _paid_batches(
    provider: str, start: datetime, end: datetime, *, lock: bool
) -> Select[tuple[BillingBatch]]:
    """The provider's paid batches created in ``[start, end)``; locked unless a
    dry run (a read-only transaction takes no row locks)."""
    marked = select(BillingTestBatch.batch_id)
    stmt = (
        select(BillingBatch)
        .where(
            BillingBatch.source == BatchSource.PURCHASE.value,
            BillingBatch.provider == provider,
            BillingBatch.provider_transaction_id.is_not(None),
            BillingBatch.id.not_in(marked),
            BillingBatch.created_at >= start,
            BillingBatch.created_at < end,
        )
        .order_by(BillingBatch.created_at, BillingBatch.id)
    )
    return stmt.with_for_update() if lock else stmt


async def sync_batches(
    session: AsyncSession,
    lookup: TransactionLookup,
    *,
    from_date: date,
    to_date: date,
    actor: str,
    dry_run: bool,
    now: datetime,
) -> SyncRun:
    """Ask the provider for the figures of its paid batches created from
    ``from_date`` to ``to_date`` (inclusive UTC dates); write what changed.

    One call of the provider's API per batch, inside the caller's transaction:
    the batches are locked until it ends — not in a dry run, which writes
    nothing and may run in a read-only transaction. A batch the API fails for
    is counted and left as it was; a second run asks it again.

    Raises:
        BillingInvalidError: no actor, or the period's start after its end.
    """
    actor = audit.require_actor(actor)
    if from_date > to_date:
        raise BillingInvalidError("the period's start is after its end")
    start, end = utc_bounds(from_date, to_date)
    batches = list(
        (
            await session.execute(
                _paid_batches(lookup.name, start, end, lock=not dry_run)
            )
        ).scalars()
    )
    updated = unchanged = not_found = other_currency = failed = 0
    for batch in batches:
        transaction_id = batch.provider_transaction_id
        assert transaction_id is not None  # the query's
        try:
            found = await lookup.transaction_figures(transaction_id)
        except PaymentProviderError as exc:
            failed += 1
            log.warning(
                "billing_provider_sync_failed",
                provider=lookup.name,
                batch_id=str(batch.id),
                error=str(exc),
            )
            continue
        if found is None:
            not_found += 1
            continue
        if found.currency != batch.currency:
            other_currency += 1
            log.warning(
                "billing_provider_sync_other_currency",
                provider=lookup.name,
                batch_id=str(batch.id),
                batch_currency=batch.currency,
                provider_currency=found.currency,
            )
            continue
        if not await _apply(
            session, batch, found, actor=actor, dry_run=dry_run, now=now
        ):
            unchanged += 1
            continue
        updated += 1
    return SyncRun(
        dry_run=dry_run,
        batches=len(batches),
        updated=updated,
        unchanged=unchanged,
        not_found=not_found,
        other_currency=other_currency,
        failed=failed,
    )


async def _apply(
    session: AsyncSession,
    batch: BillingBatch,
    found: TransactionFigures,
    *,
    actor: str,
    dry_run: bool,
    now: datetime,
) -> bool:
    """Write the provider's figures on the batch, audited; False — no change."""
    given = Figures(
        tax_minor=found.tax_minor,
        fee_minor=found.fee_minor,
        refunded_minor=found.refunded_minor,
    )
    if dry_run:
        return _merged(Figures.of(batch), given) != Figures.of(batch)
    before = figures_snapshot(batch)
    if not _write(batch, given, now):
        return False
    await session.flush()
    await audit.record(
        session,
        actor=actor,
        action=AuditAction.BATCH_PROVIDER_SYNC,
        entity="batch",
        entity_id=batch.id,
        before=before,
        after=figures_snapshot(batch),
    )
    return True
