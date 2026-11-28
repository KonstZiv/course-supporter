"""What the payment provider says of a purchase's money (Б-3, operator 2026-11-27).

Purpose:
    The partner's report (:mod:`.partner_report`) takes the tax and the
    provider's fee out of what the students paid. The journal knows what was
    paid (``billing_batches.amount_paid_minor``, tax included); the provider
    knows the rest. A table of its own keeps it, one row per purchase batch
    (``billing_batch_provider_figures``, in the batch's currency):
    ``tax_minor``, ``fee_minor``, ``refunded_minor`` and ``synced_at``. The
    batch itself stays insert-only — its trigger refuses every UPDATE (Б-5).

Two writers, one rule (:func:`_merged`):
    * the purchase's webhook — :func:`record_from_purchase`, in the
      transaction of the credit: the figures the event carries (Creem's: the
      tax, never a fee). Not audited, as the credit itself is not;
    * :func:`sync_batches` — ``scripts/billing_admin.py creem-sync``: the paid
      batches of one provider created in a period, each asked of the
      provider's API (:class:`~.payments.port.TransactionLookup`). Each batch
      that changes is audited (``batch.provider_sync``, before/after: the
      row's four columns) with the administrator's actor.

    A figure the provider gives replaces the one kept; a figure it does not
    give (None) leaves the kept one — never a guess. When nothing changes
    nothing is written, ``synced_at`` included: a second run changes nothing.
    The write is one upsert by ``batch_id`` (:func:`_upsert`) that applies the
    same rule in SQL (``COALESCE``), so two writers at once keep each other's
    figures. Figures in another currency than the batch's are not written
    (:attr:`SyncRun.other_currency`).

Only a purchase has a row: the writers check the batch (a check of the table
could not see it). A paid batch is a purchase not marked as a test one
(``billing_test_batches``) — the batches whose money the reports count
(:mod:`.review_money`).

Read by the partner's report only; nothing that charges, reserves or refunds
reads it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from course_supporter.billing import audit
from course_supporter.billing.costs import utc_bounds
from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import AuditAction, BatchSource
from course_supporter.billing.payments.port import (
    PaymentProviderError,
    TransactionFigures,
    TransactionLookup,
)
from course_supporter.storage.orm import (
    BillingBatch,
    BillingBatchProviderFigures,
    BillingTestBatch,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

_TABLE: Final = BillingBatchProviderFigures.__table__
_FIGURE_COLUMNS: Final = ("tax_minor", "fee_minor", "refunded_minor")


@dataclass(frozen=True, slots=True)
class Figures:
    """The provider's figures of a batch; None — not known."""

    tax_minor: int | None = None
    fee_minor: int | None = None
    refunded_minor: int | None = None


@dataclass(frozen=True, slots=True)
class Kept:
    """A batch's row: its figures and when they were written."""

    figures: Figures
    synced_at: datetime


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


def snapshot(kept: Kept | None) -> dict[str, Any]:
    """The row's four columns, for the audit entry of a sync."""
    figures = kept.figures if kept is not None else Figures()
    return {
        "tax_minor": figures.tax_minor,
        "fee_minor": figures.fee_minor,
        "refunded_minor": figures.refunded_minor,
        "synced_at": kept.synced_at.isoformat() if kept is not None else None,
    }


async def kept_figures(
    session: AsyncSession, batch_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Kept]:
    """The rows of these batches, by batch id; a batch without one is absent."""
    if not batch_ids:
        return {}
    rows = await session.execute(
        select(
            BillingBatchProviderFigures.batch_id,
            BillingBatchProviderFigures.tax_minor,
            BillingBatchProviderFigures.fee_minor,
            BillingBatchProviderFigures.refunded_minor,
            BillingBatchProviderFigures.synced_at,
        ).where(BillingBatchProviderFigures.batch_id.in_(batch_ids))
    )
    return {
        batch_id: Kept(Figures(tax, fee, refunded), synced)
        for batch_id, tax, fee, refunded, synced in rows.tuples()
    }


async def _upsert(
    session: AsyncSession, batch_id: uuid.UUID, given: Figures, now: datetime
) -> Kept | None:
    """Insert the batch's row, or merge ``given`` into it (:func:`_merged`, in
    SQL); the row as written, or None when it would not change."""
    row_of = insert(BillingBatchProviderFigures).values(
        batch_id=batch_id,
        tax_minor=given.tax_minor,
        fee_minor=given.fee_minor,
        refunded_minor=given.refunded_minor,
        synced_at=now,
    )
    merged = {
        name: func.coalesce(row_of.excluded[name], _TABLE.c[name])
        for name in _FIGURE_COLUMNS
    }
    upsert = row_of.on_conflict_do_update(
        index_elements=[_TABLE.c.batch_id],
        set_={**merged, "synced_at": row_of.excluded.synced_at},
        where=or_(
            *(value.is_distinct_from(_TABLE.c[name]) for name, value in merged.items())
        ),
    ).returning(*(_TABLE.c[name] for name in _FIGURE_COLUMNS), _TABLE.c.synced_at)
    row = (await session.execute(upsert)).tuples().one_or_none()
    if row is None:
        return None
    tax, fee, refunded, synced = row
    return Kept(Figures(tax, fee, refunded), synced)


async def _write(
    session: AsyncSession,
    batch_id: uuid.UUID,
    kept: Kept | None,
    given: Figures,
    now: datetime,
) -> Kept | None:
    """Upsert when the merge changes the kept figures; None — no change."""
    current = kept.figures if kept is not None else Figures()
    if _merged(current, given) == current:
        return None
    return await _upsert(session, batch_id, given, now)


async def record_from_purchase(
    session: AsyncSession,
    *,
    batch: BillingBatch,
    tax_minor: int | None,
    fee_minor: int | None,
    now: datetime,
) -> bool:
    """The figures a purchase's event carries, on its batch (module docstring).

    The caller (the webhook's transaction) commits. False when the batch is
    not a purchase, the event carries none, or the batch has them already.
    """
    if batch.source != BatchSource.PURCHASE.value:
        return False
    given = Figures(tax_minor=tax_minor, fee_minor=fee_minor)
    kept = (await kept_figures(session, {batch.id})).get(batch.id)
    return await _write(session, batch.id, kept, given, now) is not None


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
    kept = await kept_figures(session, {batch.id for batch in batches})
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
            session,
            batch.id,
            kept.get(batch.id),
            found,
            actor=actor,
            dry_run=dry_run,
            now=now,
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
    batch_id: uuid.UUID,
    kept: Kept | None,
    found: TransactionFigures,
    *,
    actor: str,
    dry_run: bool,
    now: datetime,
) -> bool:
    """Write the provider's figures of the batch, audited; False — no change."""
    given = Figures(
        tax_minor=found.tax_minor,
        fee_minor=found.fee_minor,
        refunded_minor=found.refunded_minor,
    )
    if dry_run:
        current = kept.figures if kept is not None else Figures()
        return _merged(current, given) != current
    written = await _write(session, batch_id, kept, given, now)
    if written is None:
        return False
    await audit.record(
        session,
        actor=actor,
        action=AuditAction.BATCH_PROVIDER_SYNC,
        entity="batch",
        entity_id=batch_id,
        before=snapshot(kept),
        after=snapshot(written),
    )
    return True
