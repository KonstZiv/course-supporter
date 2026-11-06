"""Reserves left hanging (BIL-B part 2): released by the scheduler.

Purpose:
    An action holds its reserve from its start to its end (``ledger.reserve``
    → ``settle`` / ``release``). If the code that should end it never runs —
    a worker killed mid-action, a bug — the units stay blocked for good. This
    function releases every ACTIVE reserve older than a threshold, by the very
    path a failed action takes (:func:`.ledger.release`, with the action's own
    kind and key): the units return, repay older differences first, and the
    journal says which action they were held for.

The threshold (``settings.billing_stale_reserve_hours``, 24 h by default) must
exceed the longest an action may legitimately run: a job runs at most
``worker_job_timeout`` (6 h) times ``worker_max_tries`` (3) = 18 h.

Nothing places reserves yet (that is BIL-E), so today this finds nothing.
Idempotent: a released reserve is no longer active. Safe beside a settle: the
account is locked and the state re-read before the release; a reserve settled
meanwhile is left alone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import ledger
from course_supporter.billing.journal import lock_account
from course_supporter.billing.kinds import ActionKind, ReservationState
from course_supporter.storage.orm import BillingReservation


@dataclass(frozen=True, slots=True)
class StaleRelease:
    """One hanging reserve, released."""

    reservation_id: uuid.UUID
    account_id: uuid.UUID
    action_kind: ActionKind
    action_key: str
    released_units: int
    created_at: datetime


async def release_stale_reservations(
    session: AsyncSession, now: datetime, *, older_than: timedelta
) -> list[StaleRelease]:
    """Release every active reserve created at or before ``now - older_than``."""
    stale = (
        await session.execute(
            select(
                BillingReservation.id,
                BillingReservation.account_id,
                BillingReservation.action_kind,
                BillingReservation.action_key,
                BillingReservation.created_at,
            )
            .where(
                BillingReservation.state == ReservationState.ACTIVE.value,
                BillingReservation.created_at <= now - older_than,
            )
            .order_by(BillingReservation.created_at, BillingReservation.id)
        )
    ).all()
    released: list[StaleRelease] = []
    for reservation_id, account_id, kind, key, created_at in stale:
        await lock_account(session, account_id)
        state = await session.scalar(
            select(BillingReservation.state).where(
                BillingReservation.id == reservation_id
            )
        )
        if state != ReservationState.ACTIVE.value:
            continue  # the action ended between the read and the lock
        done = await ledger.release(
            session, action_kind=ActionKind(kind), action_key=key, now=now
        )
        released.append(
            StaleRelease(
                reservation_id=reservation_id,
                account_id=account_id,
                action_kind=ActionKind(kind),
                action_key=key,
                released_units=done.released_units,
                created_at=created_at,
            )
        )
    return released
