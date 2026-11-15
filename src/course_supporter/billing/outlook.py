"""What the reminders say (BIL-E, part C): how long the units last, what expires.

Purpose:
    The numbers behind the reminders of the portal and of the author's app
    (operator 2026-11-15). Read-only; every threshold is a setting.

    * :func:`student_outlook` — about how many reviews the student's units
      cover: ``floor(available / the average charge of their latest
      reviews)`` over the last :data:`REVIEW_HISTORY` reviews charged to
      their account; without such history, the median price P of the latest
      review estimates of the tasks of their course (of every course they
      are enrolled in, or of the one named). ``low`` below
      ``billing_reminder_low_reviews``; ``next_may_fail`` below one. When the
      author pays for their reviews under a rule, nothing about units is said.
    * :func:`author_outlook` — about how many days the author's units last at
      the pace of the last ``billing_reminder_pace_days`` days (their charges
      over those days ÷ the days); no charges — nothing is said.
      ``runs_out_soon`` below ``billing_reminder_author_days``.
    * :func:`expiring_soon` — the first valid batch with free units that
      expires within ``billing_expiry_notice_days``: those units and its date.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import func, select

from course_supporter.billing import charging, ledger
from course_supporter.billing.journal import available, positions
from course_supporter.billing.kinds import AccountKind, ActionKind, MovementKind
from course_supporter.models.source import AssignmentType
from course_supporter.storage.orm import (
    ActionCostEstimate,
    AuthoredDocument,
    BillingMovement,
    HomeworkSubmission,
    StudentEnrollment,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.config import Settings

REVIEW_HISTORY: Final = 10
"""How many of the student's latest charged reviews make their average."""

TYPICAL_HISTORY: Final = 20
"""How many of the course's latest review estimates make the typical price."""

StudentWarning = Literal["low", "next_may_fail"]
AuthorWarning = Literal["runs_out_soon"]


@dataclass(frozen=True, slots=True)
class Expiring:
    """Free units of one batch and the day it expires."""

    units: int
    expires_on: date


@dataclass(frozen=True, slots=True)
class StudentOutlook:
    reviews_left: int | None
    warning: StudentWarning | None
    expiring_soon: Expiring | None
    paid_by: Literal["self", "author"]


@dataclass(frozen=True, slots=True)
class AuthorOutlook:
    days_left: int | None
    warning: AuthorWarning | None
    expiring_soon: Expiring | None


async def expiring_soon(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    within_days: int,
    now: datetime | None = None,
) -> Expiring | None:
    """The first valid batch with free units expiring within ``within_days``."""
    now = now or datetime.now(UTC)
    horizon = now + timedelta(days=within_days)
    for position in available(await positions(session, account_id), now):
        if position.batch.expires_at <= horizon:
            return Expiring(position.free, position.batch.expires_at.date())
        break  # positions come earliest expiry first
    return None


async def _average_review_charge(
    session: AsyncSession, account_id: uuid.UUID
) -> float | None:
    """The average units charged per review over the latest charged reviews."""
    per_review = (
        select(
            BillingMovement.action_key,
            func.sum(BillingMovement.units).label("units"),
            func.max(BillingMovement.created_at).label("at"),
        )
        .where(
            BillingMovement.account_id == account_id,
            BillingMovement.kind == MovementKind.DEBIT.value,
            BillingMovement.action_kind == ActionKind.REVIEW.value,
        )
        .group_by(BillingMovement.action_key)
        .order_by(func.max(BillingMovement.created_at).desc())
        .limit(REVIEW_HISTORY)
        .subquery()
    )
    charges = [
        int(u) for u in (await session.execute(select(per_review.c.units))).scalars()
    ]
    return sum(charges) / len(charges) if charges else None


async def _typical_review_price(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    course_id: uuid.UUID | None,
) -> float | None:
    """The median P of the latest review estimates of the student's course(s)."""
    courses = (
        [course_id]
        if course_id is not None
        else list(
            (
                await session.execute(
                    select(StudentEnrollment.course_node_id).where(
                        StudentEnrollment.student_id == student_id
                    )
                )
            ).scalars()
        )
    )
    if not courses:
        return None
    prices = [
        p
        for p in (
            await session.execute(
                select(ActionCostEstimate.p_units)
                .join(
                    HomeworkSubmission,
                    HomeworkSubmission.id == ActionCostEstimate.submission_id,
                )
                .where(
                    ActionCostEstimate.action_kind == ActionKind.REVIEW.value,
                    HomeworkSubmission.tenant_id == tenant_id,
                    HomeworkSubmission.course_node_id.in_(courses),
                    ActionCostEstimate.p_units > 0,
                )
                .order_by(ActionCostEstimate.created_at.desc())
                .limit(TYPICAL_HISTORY)
            )
        ).scalars()
        if p is not None
    ]
    if prices:
        return float(statistics.median(prices))
    return await _price_of_a_course_task(
        session, tenant_id=tenant_id, student_id=student_id, courses=courses
    )


async def _price_of_a_course_task(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    courses: list[uuid.UUID],
) -> float | None:
    """P of a review of the courses' first task at its typical size.

    The price the estimate route shows without a file (basis ``typical``,
    ``typical_submission.chars`` when the task has no history); ``None`` when
    the courses have no task answered by a file.
    """
    from course_supporter.billing.quotes import review_quote

    task = (
        await session.execute(
            select(AuthoredDocument)
            .where(
                AuthoredDocument.course_root_id.in_(courses),
                AuthoredDocument.task_type.in_(
                    [AssignmentType.TASK.value, AssignmentType.SHORT_TASK.value]
                ),
                AuthoredDocument.deleted_at.is_(None),
            )
            .order_by(AuthoredDocument.created_at, AuthoredDocument.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if task is None:
        return None
    quote = await review_quote(
        session, task=task, tenant_id=tenant_id, student_id=student_id
    )
    return float(quote.price_units) or None


async def student_outlook(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    settings: Settings,
    course_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> StudentOutlook:
    """The student's reminders (module docstring)."""
    payer = await charging.payer_of(
        session,
        action_kind=ActionKind.REVIEW,
        tenant_id=tenant_id,
        course_node_id=course_id,
        student_id=student_id,
    )
    own = payer.resolution.beneficiary_account_id
    soon = await expiring_soon(
        session, own, within_days=settings.billing_expiry_notice_days, now=now
    )
    if payer.kind is AccountKind.AUTHOR:
        return StudentOutlook(None, None, soon, "author")
    cost = await _average_review_charge(session, own)
    if cost is None:
        cost = await _typical_review_price(
            session, tenant_id=tenant_id, student_id=student_id, course_id=course_id
        )
    if not cost:
        return StudentOutlook(None, None, soon, "self")
    left = int((await ledger.balance(session, own, now=now)).available // cost)
    warning: StudentWarning | None = None
    if left < 1:
        warning = "next_may_fail"
    elif left < settings.billing_reminder_low_reviews:
        warning = "low"
    return StudentOutlook(left, warning, soon, "self")


async def author_outlook(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> AuthorOutlook:
    """The author's reminders (module docstring)."""
    now = now or datetime.now(UTC)
    soon = await expiring_soon(
        session, account_id, within_days=settings.billing_expiry_notice_days, now=now
    )
    days = settings.billing_reminder_pace_days
    charged = await session.scalar(
        select(func.coalesce(func.sum(BillingMovement.units), 0)).where(
            BillingMovement.account_id == account_id,
            BillingMovement.kind == MovementKind.DEBIT.value,
            BillingMovement.created_at >= now - timedelta(days=days),
            BillingMovement.created_at <= now,
        )
    )
    pace = int(charged or 0) / days
    if pace <= 0:
        return AuthorOutlook(None, None, soon)
    left = int((await ledger.balance(session, account_id, now=now)).available // pace)
    warning: AuthorWarning | None = (
        "runs_out_soon" if left < settings.billing_reminder_author_days else None
    )
    return AuthorOutlook(left, warning, soon)
