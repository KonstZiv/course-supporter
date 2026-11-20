"""What the reminders say (BIL-E, part C): how long the units last, what expires.

Purpose:
    The numbers behind the reminders of the portal and of the author's app
    (operator 2026-11-15). Read-only — no account is created on the way
    (BIL-E3): an owner without one reads as an empty account. Every threshold
    is a setting.

    * :func:`student_outlook` — about how many reviews the doors of a
      submission let the student through: they let a review in while
      ``available >= P`` (the price before, :mod:`.quotes`) and then charge
      its fact F, so with a typical P and F (:func:`reviews_left`)::

          N = 0                                  if available < P
          N = 1 + floor((available - P) / F)     otherwise

      P — the median of the latest :data:`TYPICAL_HISTORY` review estimates
      of the student's course (of every course they are enrolled in, or of the
      one named), each priced again from its ``e_usd`` by today's
      :func:`.charging.price_of`: the stored ``p_units`` were written at the
      rate of their day (K was cut almost by half on 2026-11-15);
      without estimates, the price of the course's first task at its typical
      size. F — the average charge of the student's own latest
      :data:`REVIEW_HISTORY` reviews; without them, the median fact of the
      course's latest finished reviews of a task (the rule the end of a
      review settles by, :func:`.charging.review_facts_usd`; facts of 0 left
      out); without those either, F = P. No P — no number. ``low`` below
      ``billing_reminder_low_reviews``; ``next_may_fail`` below one. When the
      author pays for their reviews under a rule, nothing about units is said.
      ``charging_enabled`` — charging applies to that payer
      (:func:`.charging.charging_applies`): the one ``paid_by`` names.
    * :func:`author_outlook` — about how many days the author's units last at
      the pace of the last ``billing_reminder_pace_days`` days (their charges
      over those days ÷ the days); no charges — nothing is said.
      ``runs_out_soon`` below ``billing_reminder_author_days``.
    * :func:`expiring_soon` — the first valid batch with free units that
      expires within ``billing_expiry_notice_days``: those units and its date.
"""

from __future__ import annotations

import math
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
    HomeworkStatus,
    HomeworkSubmission,
    StudentEnrollment,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.config import Settings

REVIEW_HISTORY: Final = 10
"""How many of the student's latest charged reviews make their average."""

TYPICAL_HISTORY: Final = 20
"""How many of the course's latest review estimates make the typical price,
and how many of its latest finished reviews make the typical fact."""

PAID_TASK_TYPES: Final = (AssignmentType.TASK.value, AssignmentType.SHORT_TASK.value)
"""The task types a review is paid for (a ``test`` is checked against the key)."""

FINISHED_REVIEWS: Final = (
    HomeworkStatus.COMPLETED.value,
    HomeworkStatus.DELIVERED.value,
)
"""The submission states of a review that ran to its end."""

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
    charging_enabled: bool


@dataclass(frozen=True, slots=True)
class AuthorOutlook:
    days_left: int | None
    warning: AuthorWarning | None
    expiring_soon: Expiring | None


def reviews_left(available: float, price: float, fact: float) -> int:
    """How many reviews in a row the doors let through (module docstring).

    ``price`` and ``fact`` are positive.
    """
    if available < price:
        return 0
    return 1 + math.floor((available - price) / fact)


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


async def _courses_of(
    session: AsyncSession, student_id: uuid.UUID, course_id: uuid.UUID | None
) -> list[uuid.UUID]:
    """The course named, else every course the student is enrolled in."""
    if course_id is not None:
        return [course_id]
    return list(
        (
            await session.execute(
                select(StudentEnrollment.course_node_id).where(
                    StudentEnrollment.student_id == student_id
                )
            )
        ).scalars()
    )


async def _typical_review_price(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    courses: list[uuid.UUID],
) -> float | None:
    """The median P of the latest review estimates of the courses.

    Each P is today's :func:`.charging.price_of` of the estimate's ``e_usd``,
    not its stored ``p_units``: those were written at the rate of their day.
    """
    if not courses:
        return None
    estimates = await session.execute(
        select(ActionCostEstimate.e_usd)
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
    prices = [p for p in map(charging.price_of, estimates.scalars()) if p > 0]
    if prices:
        return float(statistics.median(prices))
    return await _price_of_a_course_task(
        session, tenant_id=tenant_id, student_id=student_id, courses=courses
    )


async def _typical_review_fact(
    session: AsyncSession, *, tenant_id: uuid.UUID, courses: list[uuid.UUID]
) -> float | None:
    """The median fact F of the courses' latest finished reviews of a task.

    The latest :data:`TYPICAL_HISTORY` submissions ``completed`` or
    ``delivered`` of a ``task`` or ``short_task``; F is what the review's end
    would settle (:func:`.charging.review_facts_usd`, :func:`.charging.fact_of`).
    Facts of 0 — a copy of the same work, nothing called — are left out.
    """
    if not courses:
        return None
    finished = list(
        (
            await session.execute(
                select(HomeworkSubmission.id)
                .join(
                    AuthoredDocument,
                    AuthoredDocument.id == HomeworkSubmission.authored_document_id,
                )
                .where(
                    HomeworkSubmission.tenant_id == tenant_id,
                    HomeworkSubmission.course_node_id.in_(courses),
                    HomeworkSubmission.status.in_(FINISHED_REVIEWS),
                    AuthoredDocument.task_type.in_(PAID_TASK_TYPES),
                )
                .order_by(
                    HomeworkSubmission.created_at.desc(), HomeworkSubmission.id.desc()
                )
                .limit(TYPICAL_HISTORY)
            )
        ).scalars()
    )
    usd = await charging.review_facts_usd(session, finished)
    facts = [f for f in map(charging.fact_of, usd.values()) if f > 0]
    return float(statistics.median(facts)) if facts else None


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
    from course_supporter.billing.quotes import review_price

    task = (
        await session.execute(
            select(AuthoredDocument)
            .where(
                AuthoredDocument.course_root_id.in_(courses),
                AuthoredDocument.task_type.in_(PAID_TASK_TYPES),
                AuthoredDocument.deleted_at.is_(None),
            )
            .order_by(AuthoredDocument.created_at, AuthoredDocument.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if task is None:
        return None
    found = await review_price(
        session, task=task, tenant_id=tenant_id, student_id=student_id
    )
    return float(found.units) or None


async def student_outlook(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    settings: Settings,
    course_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> StudentOutlook:
    """The student's reminders (module docstring); creates no account."""
    payer = await charging.find_payer_of(
        session,
        action_kind=ActionKind.REVIEW,
        tenant_id=tenant_id,
        course_node_id=course_id,
        student_id=student_id,
    )
    own = payer.lookup.beneficiary_account_id
    enabled = await charging.charging_applies(session, payer.account_id, settings)
    soon = (
        await expiring_soon(
            session, own, within_days=settings.billing_expiry_notice_days, now=now
        )
        if own is not None
        else None
    )
    if payer.kind is AccountKind.AUTHOR:
        return StudentOutlook(None, None, soon, "author", enabled)
    courses = await _courses_of(session, student_id, course_id)
    price = await _typical_review_price(
        session, tenant_id=tenant_id, student_id=student_id, courses=courses
    )
    if not price:
        return StudentOutlook(None, None, soon, "self", enabled)
    fact = await _average_review_charge(session, own) if own is not None else None
    if not fact:
        fact = await _typical_review_fact(session, tenant_id=tenant_id, courses=courses)
    left = reviews_left(
        await charging.available_units(session, own, now=now), price, fact or price
    )
    warning: StudentWarning | None = None
    if left < 1:
        warning = "next_may_fail"
    elif left < settings.billing_reminder_low_reviews:
        warning = "low"
    return StudentOutlook(left, warning, soon, "self", enabled)


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
