"""Repository for StudentEnrollment operations (Phase 6 T1, KD17)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import StudentEnrollment


class StudentEnrollmentRepository:
    """Repository for student↔course access grants.

    Our side is the source of truth. ``bind`` creates a grant, ``unbind``
    deletes the row (a grant, not history). Tenant isolation is enforced at
    the API layer.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bind(
        self,
        *,
        student_id: uuid.UUID,
        course_node_id: uuid.UUID,
    ) -> StudentEnrollment:
        """Create an enrollment.

        Raises ``IntegrityError`` on a duplicate ``(student_id,
        course_node_id)`` — the route maps it to 409.
        """
        enrollment = StudentEnrollment(
            student_id=student_id,
            course_node_id=course_node_id,
        )
        self._session.add(enrollment)
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
