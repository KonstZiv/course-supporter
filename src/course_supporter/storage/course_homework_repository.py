"""Reads behind the author's overview of a course's homework (M-10).

Purpose:
    The two routes of ``api/routes/course_homework.py`` — results by task and
    the table "students by tasks" — read a whole course at once. Every read here
    is one aggregate query over the course, never one per student or per task,
    and every one is bounded by the SAME data boundary
    (:func:`_work_conditions`, :func:`_roster_conditions`):

    * the work is the tenant's, in this course (``course_node_id`` is the
      course's root, as in ``student_submissions``), on one of the course's
      tasks, and not soft-deleted;
    * the student is the tenant's, not soft-deleted, and not a service student
      (``students.is_test_student``) unless the caller asks for them — the way
      the billing cost report leaves them out.

    The module returns rows and counts; deciding what a row means (is it
    passed, what is its score) is the route's job, through the portal's own
    functions — storage knows no review.

Interface:
    :data:`TASK_TYPES` — the task types a work is expected for.
    :data:`IN_FLIGHT_STATUSES` — a submission still being worked on.
    :class:`CourseHomeworkRepository` — the reads.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import ARRAY, ColumnElement, Uuid, any_, bindparam, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.criteria_kinds import (
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.models.source import AssignmentType
from course_supporter.storage.homework_repository import ATTEMPT_STATUSES
from course_supporter.storage.orm import (
    AuthoredDocument,
    HomeworkStatus,
    HomeworkSubmission,
    Student,
    StudentCredential,
    StudentEnrollment,
    SubmissionCriterionVerdict,
    TaskCriteriaList,
    TaskCriteriaOverride,
)

TASK_TYPES: Final[tuple[str, ...]] = tuple(t.value for t in AssignmentType)
"""The documents a work is expected for: ``task``, ``short_task``, ``project``,
``test``. A material (``task_type`` NULL) is read, not answered."""

_REFUSED_OR_FAILED: Final[frozenset[str]] = frozenset(
    {
        HomeworkStatus.REJECTED.value,
        HomeworkStatus.MISMATCH.value,
        HomeworkStatus.FAILED.value,
    }
)

IN_FLIGHT_STATUSES: Final[frozenset[str]] = (
    frozenset(status.value for status in HomeworkStatus)
    - ATTEMPT_STATUSES
    - _REFUSED_OR_FAILED
)
"""A submission still in the work: every status that is neither an attempt (it
has a review) nor a refusal, an off-task verdict or a failure (it has ended
without one). The two holds — ``awaiting_funds`` and ``awaiting_criteria`` —
are in it: the work is received, and a review is still to come. Defined as
what is left, so a status added later reads as "in flight" until someone
decides it ends."""

_DECIDING_WEIGHTS: Final[tuple[str, ...]] = (
    WeightCategory.MUST.value,
    WeightCategory.SHOULD.value,
)
"""A "may" is not a fault (DD-SP-DQ): only these two weights can be "hardest"."""


@dataclass(frozen=True, slots=True)
class WorkRow:
    """One live submission as the overview reads it — no text of the work."""

    id: uuid.UUID
    student_id: uuid.UUID
    task_id: uuid.UUID
    status: str
    created_at: datetime
    score: int | None
    review_result: dict[str, Any] | None
    attempt_receipt: dict[str, Any] | None
    attempt_no: int | None


@dataclass(frozen=True, slots=True)
class CriterionTally:
    """How many of the judged last attempts missed one criterion of one list."""

    task_id: uuid.UUID
    criteria_layer: str
    criteria_source_id: uuid.UUID
    item_id: str
    weight: str
    judged: int
    not_met: int


@dataclass(frozen=True, slots=True)
class RosterRow:
    """A student of the table: the identity ``GET /students`` shows."""

    student_id: uuid.UUID
    display_name: str | None
    login: str | None


class CourseHomeworkRepository:
    """The reads of the overview; every one scoped by tenant and course."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── The course's tasks and their lists ──────────────────────────────

    async def tasks(self, course_id: uuid.UUID) -> list[AuthoredDocument]:
        """The course's live tasks, in no particular order (the route orders them).

        One read by the denormalised ``course_root_id``; whether the node a task
        hangs on is live is the tree's business, not a column of the task.
        """
        stmt = select(AuthoredDocument).where(
            AuthoredDocument.course_root_id == course_id,
            AuthoredDocument.task_type.in_(TASK_TYPES),
            AuthoredDocument.deleted_at.is_(None),
        )
        return list((await self._session.execute(stmt)).scalars())

    async def live_criteria(
        self, task_ids: list[uuid.UUID]
    ) -> tuple[
        dict[uuid.UUID, TaskCriteriaList], dict[uuid.UUID, TaskCriteriaOverride]
    ]:
        """The live model list and the live author's edit of each task, two reads."""
        if not task_ids:
            return {}, {}
        machine = await self._session.execute(
            select(TaskCriteriaList).where(
                TaskCriteriaList.authored_document_id.in_(task_ids),
                TaskCriteriaList.deleted_at.is_(None),
            )
        )
        override = await self._session.execute(
            select(TaskCriteriaOverride).where(
                TaskCriteriaOverride.authored_document_id.in_(task_ids),
                TaskCriteriaOverride.deleted_at.is_(None),
            )
        )
        return (
            {row.authored_document_id: row for row in machine.scalars()},
            {row.authored_document_id: row for row in override.scalars()},
        )

    # ── The works ───────────────────────────────────────────────────────

    async def works(
        self,
        *,
        tenant_id: uuid.UUID,
        course_id: uuid.UUID,
        task_ids: list[uuid.UUID],
        statuses: frozenset[str],
        include_test_students: bool,
        student_ids: list[uuid.UUID] | None = None,
    ) -> list[WorkRow]:
        """The course's live submissions in ``statuses``, oldest first.

        ``student_ids`` narrows the read to one page of the table. The order
        (``created_at``, ``id``) is the one the attempt ledger numbers by.
        """
        if not task_ids or student_ids == []:
            return []
        stmt = (
            select(
                HomeworkSubmission.id,
                HomeworkSubmission.student_id,
                HomeworkSubmission.authored_document_id,
                HomeworkSubmission.status,
                HomeworkSubmission.created_at,
                HomeworkSubmission.score,
                HomeworkSubmission.review_result,
                HomeworkSubmission.attempt_receipt,
                HomeworkSubmission.attempt_no,
            )
            .join(Student, Student.id == HomeworkSubmission.student_id)
            .where(
                *_work_conditions(
                    tenant_id=tenant_id,
                    course_id=course_id,
                    task_ids=task_ids,
                    statuses=statuses,
                    include_test_students=include_test_students,
                )
            )
            .order_by(HomeworkSubmission.created_at, HomeworkSubmission.id)
        )
        if student_ids is not None:
            stmt = stmt.where(HomeworkSubmission.student_id.in_(student_ids))
        return [WorkRow(*row) for row in (await self._session.execute(stmt)).all()]

    async def excluded_test_students_with_attempts(
        self, *, tenant_id: uuid.UUID, course_id: uuid.UUID, task_ids: list[uuid.UUID]
    ) -> int:
        """Service students with at least one attempt in the course."""
        if not task_ids:
            return 0
        stmt = (
            select(func.count(func.distinct(HomeworkSubmission.student_id)))
            .join(Student, Student.id == HomeworkSubmission.student_id)
            .where(
                *_work_conditions(
                    tenant_id=tenant_id,
                    course_id=course_id,
                    task_ids=task_ids,
                    statuses=ATTEMPT_STATUSES,
                    include_test_students=True,
                ),
                Student.is_test_student.is_(True),
            )
        )
        return int(await self._session.scalar(stmt) or 0)

    async def tally_criteria(
        self, *, tenant_id: uuid.UUID, submission_ids: list[uuid.UUID]
    ) -> list[CriterionTally]:
        """Per task, list and criterion: the judged attempts and the misses.

        Over the given attempts only (the route picks the one per student and
        task), criteria of weight "must" and "should" only, and only the
        criterion's own row — a mandatory point is not a criterion. The ids
        travel as one array parameter, so a large course cannot run into the
        driver's limit on bound values.
        """
        if not submission_ids:
            return []
        ids = bindparam("ids", submission_ids, type_=ARRAY(Uuid))
        verdict = SubmissionCriterionVerdict
        stmt = (
            select(
                HomeworkSubmission.authored_document_id,
                verdict.criteria_layer,
                verdict.criteria_source_id,
                verdict.item_id,
                verdict.weight,
                func.count().label("judged"),
                func.count()
                .filter(verdict.verdict == VerdictValue.NOT_MET.value)
                .label("not_met"),
            )
            .join(HomeworkSubmission, HomeworkSubmission.id == verdict.submission_id)
            .where(
                verdict.tenant_id == tenant_id,
                verdict.submission_id == any_(ids),
                verdict.item_kind == VerdictItemKind.CRITERION.value,
                verdict.weight.in_(_DECIDING_WEIGHTS),
            )
            .group_by(
                HomeworkSubmission.authored_document_id,
                verdict.criteria_layer,
                verdict.criteria_source_id,
                verdict.item_id,
                verdict.weight,
            )
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            CriterionTally(
                task_id=r[0],
                criteria_layer=r[1],
                criteria_source_id=r[2],
                item_id=r[3],
                weight=r[4],
                judged=int(r[5]),
                not_met=int(r[6]),
            )
            for r in rows
        ]

    # ── The roster of the table ─────────────────────────────────────────

    async def roster_page(
        self,
        *,
        tenant_id: uuid.UUID,
        course_id: uuid.UUID,
        task_ids: list[uuid.UUID],
        include_test_students: bool,
        limit: int,
        offset: int,
    ) -> list[RosterRow]:
        """One page of the table's students, by name (nameless last)."""
        stmt = (
            select(Student.id, Student.display_name, StudentCredential.login)
            .outerjoin(StudentCredential, StudentCredential.student_id == Student.id)
            .where(
                *_roster_conditions(
                    tenant_id=tenant_id,
                    course_id=course_id,
                    task_ids=task_ids,
                    include_test_students=include_test_students,
                )
            )
            .order_by(
                func.lower(Student.display_name).asc().nulls_last(),
                Student.id,
            )
            .limit(limit)
            .offset(offset)
        )
        return [RosterRow(*row) for row in (await self._session.execute(stmt)).all()]

    async def roster_total(
        self,
        *,
        tenant_id: uuid.UUID,
        course_id: uuid.UUID,
        task_ids: list[uuid.UUID],
        include_test_students: bool,
    ) -> int:
        """How many students the table has, all pages."""
        stmt = (
            select(func.count())
            .select_from(Student)
            .where(
                *_roster_conditions(
                    tenant_id=tenant_id,
                    course_id=course_id,
                    task_ids=task_ids,
                    include_test_students=include_test_students,
                )
            )
        )
        return int(await self._session.scalar(stmt) or 0)

    async def roster_excluded_test_students(
        self, *, tenant_id: uuid.UUID, course_id: uuid.UUID, task_ids: list[uuid.UUID]
    ) -> int:
        """Service students who would be rows of the table if they were not left out."""
        stmt = (
            select(func.count())
            .select_from(Student)
            .where(
                *_roster_conditions(
                    tenant_id=tenant_id,
                    course_id=course_id,
                    task_ids=task_ids,
                    include_test_students=True,
                ),
                Student.is_test_student.is_(True),
            )
        )
        return int(await self._session.scalar(stmt) or 0)


# ── The boundary, written once ──────────────────────────────────────────────


def _work_conditions(
    *,
    tenant_id: uuid.UUID,
    course_id: uuid.UUID,
    task_ids: list[uuid.UUID],
    statuses: frozenset[str],
    include_test_students: bool,
) -> list[ColumnElement[bool]]:
    """The data boundary of a work: the tenant's, the course's, live, in ``statuses``.

    Meant for a query that joins ``students``: the student is the tenant's and
    not soft-deleted, and a service student only when asked for.
    """
    conditions: list[ColumnElement[bool]] = [
        HomeworkSubmission.tenant_id == tenant_id,
        HomeworkSubmission.course_node_id == course_id,
        HomeworkSubmission.authored_document_id.in_(task_ids),
        HomeworkSubmission.deleted_at.is_(None),
        HomeworkSubmission.status.in_(statuses),
        Student.tenant_id == tenant_id,
        Student.deleted_at.is_(None),
    ]
    if not include_test_students:
        conditions.append(Student.is_test_student.is_not(True))
    return conditions


def _roster_conditions(
    *,
    tenant_id: uuid.UUID,
    course_id: uuid.UUID,
    task_ids: list[uuid.UUID],
    include_test_students: bool,
) -> list[ColumnElement[bool]]:
    """Who is a row of the table: enrolled in the course, or with work in it.

    "With work" is an attempt or a submission in flight on one of the course's
    tasks; a refused or failed submission alone makes no row.
    """
    enrolled = exists().where(
        StudentEnrollment.student_id == Student.id,
        StudentEnrollment.course_node_id == course_id,
    )
    submitted = exists().where(
        HomeworkSubmission.student_id == Student.id,
        HomeworkSubmission.tenant_id == tenant_id,
        HomeworkSubmission.course_node_id == course_id,
        HomeworkSubmission.authored_document_id.in_(task_ids),
        HomeworkSubmission.deleted_at.is_(None),
        HomeworkSubmission.status.in_(ATTEMPT_STATUSES | IN_FLIGHT_STATUSES),
    )
    conditions: list[ColumnElement[bool]] = [
        Student.tenant_id == tenant_id,
        Student.deleted_at.is_(None),
        enrolled | submitted,
    ]
    if not include_test_students:
        conditions.append(Student.is_test_student.is_not(True))
    return conditions
