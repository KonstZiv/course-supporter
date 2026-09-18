"""Reads and writes of student feedback (mentor-rebuild task 05).

One writer and two readers, all of them here, because every one of them has to
remember the same two filters — the submission has to be alive and so does the
student — and a rule spread over three call sites is a rule that drifts.
"""

from __future__ import annotations

import uuid
from typing import NamedTuple

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.feedback_kinds import (
    FeedbackKind,
    FeedbackTargetKind,
    FeedbackValue,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    HomeworkSubmission,
    Student,
    StudentFeedback,
)

_UNIQUE_TARGET = ("target_kind", "target_id", "student_id")
"""The index ``ON CONFLICT`` arbitrates on — the table's one-per-target rule."""


class TaskCountersRow(NamedTuple):
    """Feedback counters for one task inside a course.

    Carries the raw label fields rather than a composed label, mirroring
    :class:`~course_supporter.storage.repositories.HomeworkByTaskRow`: composing
    the display label is an api-layer projection the storage layer must not
    import. ``is_deleted`` flags a soft-deleted TASK — shown, not filtered, the
    same way the cost breakdown shows it; the two filters this module is about
    are on the submission and on the student, not on the task.
    """

    authored_document_id: uuid.UUID
    filename: str | None
    source_type: str
    order: int
    is_deleted: bool
    helped: int
    not_helped: int


class Counters(NamedTuple):
    """Feedback counters with nothing to break them down by."""

    helped: int
    not_helped: int


class FeedbackRepository:
    """Storage for what students say about what they were shown."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        tenant_id: uuid.UUID,
        student_id: uuid.UUID,
        target_kind: FeedbackTargetKind,
        target_id: uuid.UUID,
        kind: FeedbackKind,
        value: FeedbackValue,
        text: str | None = None,
    ) -> StudentFeedback:
        """Record the feedback, replacing this student's previous answer.

        One statement, not read-then-write: two requests for the same target
        can arrive together (the portal and the caller's channel are two doors
        into the same core), and only the database decides that race the same
        way every time. The unique index is what ``ON CONFLICT`` arbitrates on,
        so the rule the table declares is the rule that runs.

        ``updated_at`` is set EXPLICITLY in the update branch. SQLAlchemy does
        not apply a Python-side ``onupdate`` on this path
        (``dialects/postgresql/dml.py``: the ``set_`` dictionary "does not take
        into account Python-specified default UPDATE values"), so leaving it out
        would silently freeze the time of the first answer on every later one.

        Two execution details of this statement are load-bearing, and both were
        measured rather than assumed:

        * ``updated_at`` is set EXPLICITLY in the update branch — see above.
        * ``populate_existing`` refreshes the returned object. RETURNING rows
          are matched against the session's identity map, and an object already
          in it keeps its old values otherwise (``orm/query.py``: the option
          "force[s] all the data read from the SELECT to be populated into the
          ORM objects returned, even if these objects are already in the
          identity map"). The entry points read the student's own answer before
          writing the next one, so the object IS in the map by then: without
          this, the caller would be handed the answer it just replaced.

        Returns:
            The stored row, inserted or updated.
        """
        stmt = (
            pg_insert(StudentFeedback)
            .values(
                tenant_id=tenant_id,
                student_id=student_id,
                target_kind=target_kind.value,
                target_id=target_id,
                kind=kind.value,
                value=value.value,
                text=text,
            )
            .on_conflict_do_update(
                index_elements=list(_UNIQUE_TARGET),
                set_={
                    "value": value.value,
                    "text": text,
                    "updated_at": func.now(),
                },
            )
            .returning(StudentFeedback)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one()

    async def get_for_target(
        self,
        *,
        tenant_id: uuid.UUID,
        student_id: uuid.UUID,
        target_kind: FeedbackTargetKind,
        target_id: uuid.UUID,
    ) -> StudentFeedback | None:
        """This student's own answer about this target, or ``None``.

        Scoped by tenant as well as by student, although a student belongs to
        exactly one tenant: the invariant is that every read of feedback is
        bounded by the tenant (``impl-rules#9``), and an exemption argued from
        the shape of today's data is an exemption by form, not by area.
        """
        stmt = select(StudentFeedback).where(
            StudentFeedback.tenant_id == tenant_id,
            StudentFeedback.student_id == student_id,
            StudentFeedback.target_kind == target_kind.value,
            StudentFeedback.target_id == target_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def counters_by_task(
        self,
        *,
        tenant_id: uuid.UUID,
        course_node_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskCountersRow]:
        """Touch counters for one course, grouped by task.

        A foreign or unknown course returns an empty list rather than an error
        — the same leak-safe answer the cost breakdown gives, for the same
        reason: an author must not learn which courses exist outside their
        tenant by comparing error codes.
        """
        stmt = (
            self._touches_on_live_reviews()
            .add_columns(
                AuthoredDocument.id.label("authored_document_id"),
                AuthoredDocument.filename.label("filename"),
                AuthoredDocument.source_type.label("source_type"),
                AuthoredDocument.order.label("order"),
                AuthoredDocument.deleted_at.label("deleted_at"),
            )
            .join(
                AuthoredDocument,
                HomeworkSubmission.authored_document_id == AuthoredDocument.id,
            )
            .where(
                HomeworkSubmission.tenant_id == tenant_id,
                HomeworkSubmission.course_node_id == course_node_id,
            )
            .group_by(
                AuthoredDocument.id,
                AuthoredDocument.filename,
                AuthoredDocument.source_type,
                AuthoredDocument.order,
                AuthoredDocument.deleted_at,
            )
            .order_by(AuthoredDocument.order)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            TaskCountersRow(
                authored_document_id=row.authored_document_id,
                filename=row.filename,
                source_type=row.source_type,
                order=row.order,
                is_deleted=row.deleted_at is not None,
                helped=int(row.helped),
                not_helped=int(row.not_helped),
            )
            for row in result.all()
        ]

    async def counters_for_task(
        self,
        *,
        tenant_id: uuid.UUID,
        authored_document_id: uuid.UUID,
    ) -> Counters:
        """Touch counters for one task, summed over every student.

        A foreign or unknown task counts zero — leak-safe for the same reason
        :meth:`counters_by_task` returns an empty list.
        """
        stmt = self._touches_on_live_reviews().where(
            HomeworkSubmission.tenant_id == tenant_id,
            HomeworkSubmission.authored_document_id == authored_document_id,
        )
        row = (await self._session.execute(stmt)).one()
        return Counters(helped=int(row.helped), not_helped=int(row.not_helped))

    @staticmethod
    def _touches_on_live_reviews() -> Select[tuple[int, int]]:
        """Counted touches on reviews, with the two filters both readers need.

        The filters are separate conditions because they are separate facts: a
        submission can be soft-deleted on its own, and a student can be
        soft-deleted while their submissions stay as they were — the cascade
        from student to submissions only runs when the deletion goes through
        ``CascadeDeleteService``, and direct SQL (the prod clean-up of
        2026-09-17) does not.

        The join to the student is what enforces the second filter, so it is an
        inner join and not an outer one: a row whose student is gone must fall
        out of the count, not count with a NULL beside it.
        """
        helped = func.count().filter(StudentFeedback.value == FeedbackValue.HELPED)
        not_helped = func.count().filter(
            StudentFeedback.value == FeedbackValue.NOT_HELPED
        )
        return (
            select(
                helped.label("helped"),
                not_helped.label("not_helped"),
            )
            .select_from(StudentFeedback)
            .join(
                HomeworkSubmission,
                StudentFeedback.target_id == HomeworkSubmission.id,
            )
            .join(Student, StudentFeedback.student_id == Student.id)
            .where(
                StudentFeedback.target_kind == FeedbackTargetKind.REVIEW,
                StudentFeedback.kind == FeedbackKind.TOUCH,
                HomeworkSubmission.deleted_at.is_(None),
                Student.deleted_at.is_(None),
            )
        )
