"""A payment provider's webhook: each event in a transaction of its own (BIL-PAY).

Purpose:
    The provider tells what happened; the journal follows once per event,
    however many times the provider repeats it. The route
    (``POST /api/v1/billing/webhooks/{provider}``) checks nothing but the
    signature (the adapter's :meth:`~.port.PaymentProvider.parse_webhook`)
    and hands the events here.

One event, one transaction:
    1. The event is locked by its key, ``(provider, provider_event_id)`` — a
       transaction-scoped advisory lock — so two deliveries of one event
       never run side by side: the second waits and finds the first's row.
    2. A row of ``billing_provider_events`` already there, and not failed: a
       repeat. Nothing changes, and the provider is answered 200.
    3. The journal's action:

       * :class:`~.port.PurchaseCompleted` — :func:`.ledger.credit_purchase`
         to the account the custom data names: the units of the package from
         the catalog (on sale or not by now — it was paid for), the amount
         and currency the provider says were paid, its transaction and line.
         No such account, or a closed one: the event FAILS with a WARNING and
         is answered 200 — the provider repeating it would not help; the row
         waits for a person.
       * :class:`~.port.RefundApproved` / :class:`~.port.RefundRejected` —
         :func:`.refunds.refund_approve` / :func:`.refunds.refund_reject` of
         the provider's adjustment.
       * :class:`~.port.Chargeback` — RECORDED, with a WARNING
         ``billing_chargeback``; the journal is not touched (what a chargeback
         does to the units is not decided).
       * :class:`~.port.Unknown` — IGNORED.

    4. The row is written with the outcome — the normalized fields, the
       SHA-256 of the body, never the body — and committed with the action.

A failure:
    Anything raised in 1-4 rolls the transaction back, the journal's action
    with it; the event is then written as FAILED — what failed, without
    personal data — in a transaction of its own, and
    :class:`EventHandlingError` tells the route to answer 500, so the provider
    sends the event again. A failed row is handled afresh when the event comes
    again (point 2 is for the other states).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, assert_never

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import ledger, refunds
from course_supporter.billing.errors import BillingError, BillingNotFoundError
from course_supporter.billing.kinds import AccountStatus, EventState
from course_supporter.billing.payments.catalog import Catalog
from course_supporter.billing.payments.port import (
    Chargeback,
    ProviderEvent,
    PurchaseCompleted,
    RefundApproved,
    RefundRejected,
    Unknown,
)
from course_supporter.storage.orm import BillingAccount, BillingProviderEvent

log = structlog.get_logger()

_MAX_ERROR: Final = 1000
_MAX_PROVIDER_TYPE: Final = 64


@dataclass(frozen=True, slots=True)
class Handled:
    """What became of one event."""

    event_id: str
    state: EventState
    repeated: bool
    """True when the event was handled before: nothing changed now."""


class EventHandlingError(Exception):
    """An event failed: its action was rolled back and its row says why.

    The events before it in the same body were handled and committed.
    """


async def handle_events(
    session: AsyncSession,
    *,
    provider: str,
    events: Sequence[ProviderEvent],
    body_sha256: str,
    catalog: Catalog,
    now: datetime | None = None,
) -> list[Handled]:
    """Handle each event in a transaction of its own (module docstring).

    Commits and rolls back ``session`` itself: give it a session with nothing
    to keep.

    Raises:
        EventHandlingError: an event failed; it is recorded as failed.
    """
    handled: list[Handled] = []
    for event in events:
        at = now or datetime.now(UTC)
        try:
            outcome = await _handle(
                session,
                provider=provider,
                event=event,
                body_sha256=body_sha256,
                catalog=catalog,
                now=at,
            )
            await session.commit()
        except Exception as exc:
            await session.rollback()
            error = _describe(exc)
            log.exception(
                "billing_event_failed",
                provider=provider,
                event_id=event.event_id,
                event_type=event.kind.value,
                error=error,
            )
            await _record_failure(
                session,
                provider=provider,
                event=event,
                body_sha256=body_sha256,
                error=error,
                now=at,
            )
            await session.commit()
            msg = f"{provider} event {event.event_id} failed: {error}"
            raise EventHandlingError(msg) from exc
        log.info(
            "billing_event_handled",
            provider=provider,
            event_id=event.event_id,
            event_type=event.kind.value,
            state=outcome.state.value,
            repeated=outcome.repeated,
        )
        handled.append(outcome)
    return handled


# ── One event ──────────────────────────────────────────────────────────


async def _handle(
    session: AsyncSession,
    *,
    provider: str,
    event: ProviderEvent,
    body_sha256: str,
    catalog: Catalog,
    now: datetime,
) -> Handled:
    await _lock(session, provider, event.event_id)
    row = await _row(session, provider, event.event_id)
    if row is not None and row.state != EventState.FAILED.value:
        return Handled(event.event_id, EventState(row.state), repeated=True)
    state, error = await _act(
        session, provider=provider, event=event, catalog=catalog, now=now
    )
    await _write(
        session,
        row=row,
        provider=provider,
        event=event,
        body_sha256=body_sha256,
        state=state,
        error=error,
        now=now,
    )
    return Handled(event.event_id, state, repeated=False)


async def _act(
    session: AsyncSession,
    *,
    provider: str,
    event: ProviderEvent,
    catalog: Catalog,
    now: datetime,
) -> tuple[EventState, str | None]:
    """The journal's action; the event's state and, when it failed, why."""
    match event:
        case PurchaseCompleted():
            return await _credit(session, provider, event, catalog, now)
        case RefundApproved():
            await refunds.refund_approve(
                session,
                provider=provider,
                provider_adjustment_id=event.adjustment_id,
                now=now,
            )
            return EventState.PROCESSED, None
        case RefundRejected():
            await refunds.refund_reject(
                session,
                provider=provider,
                provider_adjustment_id=event.adjustment_id,
                now=now,
            )
            return EventState.PROCESSED, None
        case Chargeback():
            log.warning(
                "billing_chargeback",
                provider=provider,
                event_id=event.event_id,
                account_id=str(event.account_id) if event.account_id else None,
                transaction_id=event.transaction_id,
                amount_minor=event.amount_minor,
                currency=event.currency,
            )
            return EventState.RECORDED, None
        case Unknown():
            return EventState.IGNORED, None
    assert_never(event)


async def _credit(
    session: AsyncSession,
    provider: str,
    event: PurchaseCompleted,
    catalog: Catalog,
    now: datetime,
) -> tuple[EventState, str | None]:
    account = (
        await session.get(BillingAccount, event.account_id)
        if event.account_id is not None
        else None
    )
    if account is None or account.status != AccountStatus.ACTIVE.value:
        if event.account_id is None:
            why = "the purchase names no account"
        elif account is None:
            why = f"account {event.account_id} not found"
        else:
            why = f"account {event.account_id} is closed"
        log.warning(
            "billing_purchase_without_account",
            provider=provider,
            event_id=event.event_id,
            reason=why,
            transaction_id=event.transaction_id,
        )
        return EventState.FAILED, f"{why}: nothing credited, for a person to settle"
    package = catalog.package(event.package_id)
    if package is None:
        raise BillingNotFoundError(
            f"package {event.package_id!r} is not in the catalog"
        )
    await ledger.credit_purchase(
        session,
        account_id=account.id,
        units=package.units,
        amount_paid_minor=event.amount_paid_minor,
        currency=event.currency,
        provider=provider,
        provider_transaction_id=event.transaction_id,
        provider_line_id=event.line_id,
        now=now,
    )
    return EventState.PROCESSED, None


# ── The row ────────────────────────────────────────────────────────────


async def _lock(session: AsyncSession, provider: str, event_id: str) -> None:
    """Hold the event's key until the transaction ends (module docstring, 1)."""
    key = func.hashtextextended(f"billing_provider_event:{provider}:{event_id}", 0)
    await session.execute(select(func.pg_advisory_xact_lock(key)))


async def _row(
    session: AsyncSession, provider: str, event_id: str
) -> BillingProviderEvent | None:
    return (
        await session.execute(
            select(BillingProviderEvent)
            .where(
                BillingProviderEvent.provider == provider,
                BillingProviderEvent.provider_event_id == event_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _fields(event: ProviderEvent) -> dict[str, Any]:
    """The event's normalized fields, as the row keeps them."""
    account_id: uuid.UUID | None = None
    fields: dict[str, Any] = {
        "event_type": event.kind.value,
        "provider_event_type": event.provider_type[:_MAX_PROVIDER_TYPE],
        "occurred_at": event.occurred_at,
        "package_id": None,
        "amount_minor": None,
        "currency": None,
        "provider_transaction_id": None,
        "provider_line_id": None,
        "provider_adjustment_id": None,
    }
    match event:
        case PurchaseCompleted():
            account_id = event.account_id
            fields |= {
                "package_id": event.package_id,
                "amount_minor": event.amount_paid_minor,
                "currency": event.currency,
                "provider_transaction_id": event.transaction_id,
                "provider_line_id": event.line_id,
            }
        case RefundApproved() | RefundRejected():
            fields |= {
                "amount_minor": event.amount_minor,
                "currency": event.currency,
                "provider_transaction_id": event.transaction_id,
                "provider_adjustment_id": event.adjustment_id,
            }
        case Chargeback():
            account_id = event.account_id
            fields |= {
                "amount_minor": event.amount_minor,
                "currency": event.currency,
                "provider_transaction_id": event.transaction_id,
            }
        case Unknown():
            pass
    return fields | {"account_id": account_id}


async def _write(
    session: AsyncSession,
    *,
    row: BillingProviderEvent | None,
    provider: str,
    event: ProviderEvent,
    body_sha256: str,
    state: EventState,
    error: str | None,
    now: datetime,
) -> None:
    values = _fields(event) | {
        "state": state.value,
        "error": error,
        "body_sha256": body_sha256,
        "processed_at": now,
    }
    if row is None:
        session.add(
            BillingProviderEvent(
                provider=provider,
                provider_event_id=event.event_id,
                received_at=now,
                **values,
            )
        )
    else:
        await session.execute(
            update(BillingProviderEvent)
            .where(BillingProviderEvent.id == row.id)
            .values(**values)
        )
    await session.flush()


async def _record_failure(
    session: AsyncSession,
    *,
    provider: str,
    event: ProviderEvent,
    body_sha256: str,
    error: str,
    now: datetime,
) -> None:
    """Write the event as failed, unless another delivery handled it meanwhile."""
    await _lock(session, provider, event.event_id)
    row = await _row(session, provider, event.event_id)
    if row is not None and row.state != EventState.FAILED.value:
        return
    await _write(
        session,
        row=row,
        provider=provider,
        event=event,
        body_sha256=body_sha256,
        state=EventState.FAILED,
        error=error,
        now=now,
    )


def _describe(exc: BaseException) -> str:
    """What failed, for the row: billing's own refusals keep their words (ids
    and figures, never a name or an address); anything else is named by its
    class only, since its message may quote values."""
    if isinstance(exc, BillingError):
        return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR]
    return type(exc).__name__
