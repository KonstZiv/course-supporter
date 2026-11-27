"""Repository for StudentEnrollment operations (Phase 6 T1, KD17)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import CourseNode, EnrollmentEvent, StudentEnrollment


class StudentEnrollmentRepository:
    """Repository for student↔course access grants.

    Our side is the source of truth. ``bind`` creates a grant together with
    its ``EnrollmentEvent`` (the funnel's fact, Б-4); ``unbind`` deletes the
    grant row (a grant, not history) and leaves the event. Tenant isolation
    is enforced at the API layer.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bind(
        self,
        *,
        tenant_id: uuid.UUID,
        student_id: uuid.UUID,
        course_node_id: uuid.UUID,
        source: str,
        source_tag: str | None = None,
    ) -> StudentEnrollment:
        """Create an enrollment and its event in the caller's transaction.

        The one door for a new grant: an author's (``source='author'``) and a
        self-enrollment (``'self'``, with the join link's ``source_tag``).
        Both rows are flushed together, so a rollback — or a failed savepoint
        around this call — leaves neither.

        Raises ``IntegrityError`` on a duplicate ``(student_id,
        course_node_id)`` — the route maps it to 409.
        """
        enrollment = StudentEnrollment(
            student_id=student_id,
            course_node_id=course_node_id,
            source=source,
        )
        self._session.add(enrollment)
        self._session.add(
            EnrollmentEvent(
                tenant_id=tenant_id,
                course_node_id=course_node_id,
                student_id=student_id,
                source=source,
                source_tag=source_tag,
            )
        )
        await self._session.flush()
        return enrollment

    async def unbind(
        self,
        *,
        student_id: uuid.UUID,
        course_node_id: uuid.UUID,
    ) -> bool:
        """Delete an enrollment (revoke course access).

        Returns True if a row was deleted, False if the pair was not bound.
        """
        stmt = delete(StudentEnrollment).where(
            StudentEnrollment.student_id == student_id,
            StudentEnrollment.course_node_id == course_node_id,
        )
        result: CursorResult[Any] = await self._session.execute(stmt)  # type: ignore[assignment]
        await self._session.flush()
        return result.rowcount > 0

    async def list_for_student(
        self,
        student_id: uuid.UUID,
    ) -> list[StudentEnrollment]:
        """List a student's enrollments, oldest first."""
        stmt = (
            select(StudentEnrollment)
            .where(StudentEnrollment.student_id == student_id)
            .order_by(StudentEnrollment.created_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_enrolled_courses(
        self,
        student_id: uuid.UUID,
    ) -> list[CourseNode]:
        """List the root CourseNodes a student is enrolled in (portal listing).

        JOINs enrollments → course_nodes and returns only ACTIVE roots
        (``deleted_at IS NULL``), ordered oldest-enrollment-first (mirrors
        :meth:`list_for_student`). One query — no N+1 (Phase 6 T4a c1, Q4).
        The enrollment grant is itself the publish gate (publish-gate A, KD17):
        a student sees exactly the courses they are enrolled in. Root-ness is
        guaranteed by the bind route (enrollments only ever bind a root).
        """
        stmt = (
            select(CourseNode)
            .join(
                StudentEnrollment,
                StudentEnrollment.course_node_id == CourseNode.id,
            )
            .where(
                StudentEnrollment.student_id == student_id,
                CourseNode.deleted_at.is_(None),
            )
            .order_by(StudentEnrollment.created_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def is_enrolled(
        self,
        student_id: uuid.UUID,
        course_node_id: uuid.UUID,
    ) -> bool:
        """Return True if the student is enrolled in the course (root node).

        The enrollment grant is at the ROOT-CourseNode (course) level (KD17),
        so ``course_node_id`` is a course root — for a submission, the task's
        ``course_root_id``. Gates the portal submission point (Phase 6 T2, Q1):
        a student may only submit to a task whose course they are enrolled in.
        """
        stmt = (
            select(StudentEnrollment.id)
            .where(
                StudentEnrollment.student_id == student_id,
                StudentEnrollment.course_node_id == course_node_id,
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.first() is not None
