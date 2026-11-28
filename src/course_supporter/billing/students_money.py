"""What the students paid for the reviews, by course and student (BIL-COSTS Б-1).

The author's (and the school's) view of which student and which course pay the
most (operator 2026-11-26): the money of the students' own accounts spent on
reviews in a period, the biggest first. The administrator's cost report counts
the same (:mod:`.costs`), and both count it in :mod:`.review_money` — the rules
of the money (a paid batch's share, gift units, service students, refunds) are
there, not here.

What is here:

* only reviews the student paid from their own account; a review the author
  pays for under a rule is not a student's money and is not shown;
* service students (``students.is_test_student``) are never shown; their
  reviews are counted in :attr:`StudentsMoney.excluded_test_reviews`;
* ``units`` — the units debited from the students' accounts for the reviews:
  the units of paid batches and the ``gift_units`` together;
* a course is the root of the review's task (:func:`.links.link_actions`) and
  must be the tenant's own; a review that leads to no course of the tenant is
  in :attr:`StudentsMoney.unlinked` — in the totals, in no course, never lost;
* a course or a student without reviews in the period is not shown; both are
  ordered by ``units``, the biggest first (units do not depend on the
  currency), then by id;
* a deleted student (``students.deleted_at``) keeps their row, without a name.

Read-only; every query carries the tenant (``impl-rules#9``); the period is
inclusive UTC dates.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import select

from course_supporter.billing.costs import utc_bounds
from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import ActionKind
from course_supporter.billing.links import ActionRef, link_actions
from course_supporter.billing.review_money import (
    ReviewTally,
    excluded_test_reviews,
    review_debits,
)
from course_supporter.storage.orm import CourseNode, Student

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class MoneyFigures:
    """What students paid for some reviews (:mod:`.review_money`)."""

    reviews: int = 0
    units: int = 0
    money_minor: tuple[tuple[str, int], ...] = ()
    gift_units: int = 0

    @classmethod
    def of(cls, tally: ReviewTally) -> MoneyFigures:
        """The figures of a tally; the money is rounded once, here."""
        return cls(
            reviews=len(tally.keys),
            units=tally.units,
            money_minor=tally.money_minor(),
            gift_units=tally.gift_units,
        )


@dataclass(frozen=True, slots=True)
class StudentMoney:
    """A student of a course: what they paid for reviews of it."""

    student_id: uuid.UUID
    display_name: str | None
    deleted: bool
    figures: MoneyFigures


@dataclass(frozen=True, slots=True)
class CourseMoney:
    """A course: what its students paid for reviews, and each of them."""

    course_node_id: uuid.UUID
    title: str | None
    figures: MoneyFigures
    students: list[StudentMoney] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StudentsMoney:
    """The students' money of a tenant in a period (module docstring)."""

    from_date: date
    to_date: date
    totals: MoneyFigures
    courses: list[CourseMoney]
    unlinked: MoneyFigures
    excluded_test_reviews: int


async def _own_courses(
    session: AsyncSession, tenant_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Titles of the tenant's own course nodes among ``ids``."""
    if not ids:
        return {}
    rows = await session.execute(
        select(CourseNode.id, CourseNode.title).where(
            CourseNode.tenant_id == tenant_id, CourseNode.id.in_(ids)
        )
    )
    return dict(rows.tuples().all())


async def _own_students(
    session: AsyncSession, tenant_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str | None, bool]]:
    """Name and ``deleted`` of the tenant's own students among ``ids``."""
    if not ids:
        return {}
    rows = await session.execute(
        select(Student.id, Student.display_name, Student.deleted_at).where(
            Student.tenant_id == tenant_id, Student.id.in_(ids)
        )
    )
    return {
        student_id: (None if deleted_at is not None else name, deleted_at is not None)
        for student_id, name, deleted_at in rows.tuples()
    }


def _by_units_then_id(
    items: dict[uuid.UUID, ReviewTally],
) -> list[tuple[uuid.UUID, ReviewTally]]:
    return sorted(items.items(), key=lambda item: (-item[1].units, str(item[0])))


async def students_money(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    from_date: date,
    to_date: date,
) -> StudentsMoney:
    """The students' money of the tenant from ``from_date`` to ``to_date``.

    Raises:
        BillingInvalidError: ``from_date`` is after ``to_date``.
    """
    if from_date > to_date:
        raise BillingInvalidError("the period's start is after its end")
    start, end = utc_bounds(from_date, to_date)
    debits = await review_debits(
        session,
        start_dt=start,
        end_dt_exclusive=end,
        tenant_id=tenant_id,
        include_test_students=False,
    )
    excluded = await excluded_test_reviews(
        session, start_dt=start, end_dt_exclusive=end, tenant_id=tenant_id
    )
    links = await link_actions(
        session, {ActionRef(ActionKind.REVIEW, debit.action_key) for debit in debits}
    )
    titles = await _own_courses(
        session,
        tenant_id,
        {
            course
            for link in links.values()
            if (course := link.course_node_id) is not None
        },
    )

    totals, unlinked = ReviewTally(), ReviewTally()
    per_course: dict[uuid.UUID, dict[uuid.UUID, ReviewTally]] = defaultdict(dict)
    course_tallies: dict[uuid.UUID, ReviewTally] = defaultdict(ReviewTally)
    for debit in debits:
        totals.add(debit)
        course = links[ActionRef(ActionKind.REVIEW, debit.action_key)].course_node_id
        if course is None or course not in titles or debit.student_id is None:
            unlinked.add(debit)
            continue
        course_tallies[course].add(debit)
        per_course[course].setdefault(debit.student_id, ReviewTally()).add(debit)

    students = await _own_students(
        session,
        tenant_id,
        {student for per_student in per_course.values() for student in per_student},
    )
    courses = [
        CourseMoney(
            course_node_id=course,
            title=titles[course],
            figures=MoneyFigures.of(course_tallies[course]),
            students=[
                StudentMoney(
                    student_id=student,
                    display_name=students.get(student, (None, True))[0],
                    deleted=students.get(student, (None, True))[1],
                    figures=MoneyFigures.of(student_tally),
                )
                for student, student_tally in _by_units_then_id(per_course[course])
            ],
        )
        for course, _ in _by_units_then_id(course_tallies)
    ]
    return StudentsMoney(
        from_date=from_date,
        to_date=to_date,
        totals=MoneyFigures.of(totals),
        courses=courses,
        unlinked=MoneyFigures.of(unlinked),
        excluded_test_reviews=excluded,
    )
