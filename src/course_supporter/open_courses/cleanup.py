"""The periodic deletion of self-enrollment requests (O-1).

    portal_signups_cleanup — requests never confirmed and past their
                             ``expires_at`` (7 days) are deleted; confirmed
                             requests older than
                             :data:`~.links.CONFIRMED_SIGNUP_RETENTION` (30 days)
                             are deleted too, so their password hash is not
                             kept. Daily (UTC).

The lock, the transaction and the one log line are the runner's
(``course_supporter.periodic.runner``); the counts go into its line.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.open_courses.links import CONFIRMED_SIGNUP_RETENTION
from course_supporter.periodic.runner import PeriodicTask, TaskContext, TaskResult
from course_supporter.storage.orm import PortalSignup

if TYPE_CHECKING:
    from course_supporter.config import Settings


async def delete_old_signups(session: AsyncSession, now: datetime) -> tuple[int, int]:
    """Delete expired unconfirmed and old confirmed requests; return the two
    counts (expired, confirmed)."""
    expired: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
        delete(PortalSignup).where(
            PortalSignup.confirmed_at.is_(None), PortalSignup.expires_at < now
        )
    )
    confirmed: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
        delete(PortalSignup).where(
            PortalSignup.confirmed_at.is_not(None),
            PortalSignup.created_at < now - CONFIRMED_SIGNUP_RETENTION,
        )
    )
    return expired.rowcount, confirmed.rowcount


async def _cleanup(run: TaskContext) -> TaskResult:
    async with run.transaction() as session:
        expired, confirmed = await delete_old_signups(session, run.now)
    return TaskResult(expired + confirmed, {"expired": expired, "confirmed": confirmed})


def _enabled(s: Settings) -> bool:
    return s.periodic_portal_signups_cleanup_enabled


def _cron(s: Settings) -> str:
    return s.periodic_portal_signups_cleanup_cron


PORTAL_SIGNUPS_CLEANUP = PeriodicTask(
    name="portal_signups_cleanup",
    body=_cleanup,
    enabled=_enabled,
    cron=_cron,
    summary="delete expired self-enrollment requests and confirmed ones after 30 days",
)

OPEN_COURSE_TASKS: tuple[PeriodicTask, ...] = (PORTAL_SIGNUPS_CLEANUP,)
