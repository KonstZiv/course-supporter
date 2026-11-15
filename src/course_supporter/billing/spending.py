"""What an author's actions cost them, in units, by course and task (BIL-E2).

Purpose:
    The author's "Spending" page (operator 2026-11-15, 5b item 1): what was
    charged to the author's account for actions in a period, with the courses
    and the homework tasks it went to. Units only — what the platform pays in
    dollars is the operator's (:mod:`.costs`), never here.

What counts (the author's account, movements of an action only):

    * billed — ``debit`` + ``difference_arisen``: what was charged to the
      author (a difference is charged too, only later repaid);
    * covered — ``benefit_covered`` + ``platform_covered``: what the
      author's benefit or the platform bore — shown apart, never charged.

    Purchases, refunds, expiry and reserves are not spending. Every number is
    positive. A student's review the author pays for under a rule is under
    its task, like any other.

Where it goes — :func:`.links.link_actions`:

    * a course: ``reviews_units``, ``processing_units`` and
      ``preparation_units`` split its billed units by kind of action;
    * a task of the course: its reviews (how many) and preparations, billed
      and covered — a processing is the course's, not a task's;
    * an action that leads to no course: ``unlinked``.

Read-only; the period is inclusive UTC dates (:func:`.costs.default_month_bounds`
for the current month).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from course_supporter.billing.costs import (
    BILLED_KINDS,
    COVERED_KINDS,
    UnitsSummary,
    course_titles,
    task_titles,
    utc_bounds,
)
from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import ActionKind
from course_supporter.billing.links import ActionRef, link_actions
from course_supporter.storage.orm import BillingMovement

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(slots=True)
class TaskSpending:
    """A homework task: its reviews and preparations."""

    authored_document_id: uuid.UUID
    title: str | None
    reviews: int = 0
    units: UnitsSummary = field(default_factory=UnitsSummary)


@dataclass(slots=True)
class CourseSpending:
    """A course: all its actions, by kind, and its tasks."""

    course_node_id: uuid.UUID
    title: str | None
    units: UnitsSummary = field(default_factory=UnitsSummary)
    by_kind: dict[ActionKind, int] = field(
        default_factory=lambda: dict.fromkeys(ActionKind, 0)
    )
    tasks: list[TaskSpending] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Spending:
    """An author's spending in a period."""

    from_date: date
    to_date: date
    totals: UnitsSummary
    courses: list[CourseSpending]
    unlinked: UnitsSummary


async def author_spending(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    from_date: date,
    to_date: date,
) -> Spending:
    """The author's spending from ``from_date`` to ``to_date`` (module docstring).

    Raises:
        BillingInvalidError: ``from_date`` is after ``to_date``.
    """
    if from_date > to_date:
        raise BillingInvalidError("the period's start is after its end")
    start, end = utc_bounds(from_date, to_date)
    rows = (
        await session.execute(
            select(
                BillingMovement.action_kind,
                BillingMovement.action_key,
                BillingMovement.kind,
                func.sum(BillingMovement.units),
            )
            .where(
                BillingMovement.account_id == account_id,
                BillingMovement.kind.in_(BILLED_KINDS | COVERED_KINDS),
                BillingMovement.action_key.is_not(None),
                BillingMovement.created_at >= start,
                BillingMovement.created_at < end,
            )
            .group_by(
                BillingMovement.action_kind,
                BillingMovement.action_key,
                BillingMovement.kind,
            )
        )
    ).tuples()
    moved = [
        (ActionRef(ActionKind(action_kind), key), kind, int(units))
        for action_kind, key, kind, units in rows
        if action_kind is not None and key is not None
    ]
    links = await link_actions(session, (ref for ref, _, _ in moved))

    totals, unlinked = UnitsSummary(), UnitsSummary()
    courses: dict[uuid.UUID, CourseSpending] = {}
    tasks: dict[uuid.UUID, dict[uuid.UUID, TaskSpending]] = defaultdict(dict)
    reviewed: dict[uuid.UUID, set[str]] = defaultdict(set)
    for ref, kind, units in moved:
        these = UnitsSummary.of(kind, units)
        totals = totals + these
        link = links[ref]
        if link.course_node_id is None:
            unlinked = unlinked + these
            continue
        course = courses.setdefault(
            link.course_node_id, CourseSpending(link.course_node_id, None)
        )
        course.units = course.units + these
        course.by_kind[ref.kind] += these.billed_units
        task_id = link.authored_document_id
        if ref.kind is ActionKind.PROCESSING or task_id is None:
            continue
        task = tasks[link.course_node_id].setdefault(
            task_id, TaskSpending(task_id, None)
        )
        task.units = task.units + these
        if ref.kind is ActionKind.REVIEW:
            reviewed[task_id].add(ref.key)

    titles = await course_titles(session, set(courses))
    names = await task_titles(
        session, {task for per_course in tasks.values() for task in per_course}
    )
    for course_id, course in courses.items():
        course.title = titles.get(course_id)
        for task in tasks[course_id].values():
            task.title = names.get(task.authored_document_id)
            task.reviews = len(reviewed[task.authored_document_id])
        course.tasks = sorted(
            tasks[course_id].values(),
            key=lambda t: (t.title or "", str(t.authored_document_id)),
        )
    ordered = sorted(
        courses.values(), key=lambda c: (c.title or "", str(c.course_node_id))
    )
    return Spending(from_date, to_date, totals, ordered, unlinked)
