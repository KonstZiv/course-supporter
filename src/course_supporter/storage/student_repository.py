"""Repository for Student CRUD operations."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import Student


class StudentRepository:
    """Repository for student operations.

    Students are identified by (tenant_id, external_id) pair.
    Uses get-or-create pattern since external systems may submit
    for the same student multiple times.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self,
        *,
        tenant_id: uuid.UUID,
        external_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Student, bool]:
        """Get existing student or create a new one.

        Args:
            tenant_id: Owning tenant UUID.
            external_id: Student identifier from the caller's system.
            metadata: Optional arbitrary metadata about the student.

        Returns:
            Tuple of (student, created) where created is True if new.
        """
        existing = await self.get_by_external_id(tenant_id, external_id)
        if existing is not None:
            return existing, False

        try:
            async with self._session.begin_nested():
                student = Student(
                    tenant_id=tenant_id,
                    external_id=external_id,
                    metadata_=metadata,
                )
                self._session.add(student)
                await self._session.flush()
                return student, True
        except IntegrityError:
            # Concurrent insert won the race — fetch the winner
            existing = await self.get_by_external_id(tenant_id, external_id)
            if existing is None:
                raise  # Unexpected integrity error, not a duplicate
            return existing, False

    async def create(
        self,
        *,
        tenant_id: uuid.UUID,
        external_id: str,
        display_name: str | None = None,
    ) -> Student:
        """Explicitly create a student (portal provisioning, Phase 6 T1).

        Distinct from :meth:`get_or_create`: this is the generate/manual
        ``external_id`` provisioning path and lets a duplicate
        ``(tenant_id, external_id)`` raise ``IntegrityError`` (the route maps
        it to 409) rather than silently returning the existing row.

        Args:
            tenant_id: Owning tenant UUID.
            external_id: Student identifier (generated or admin-provided).
            display_name: Optional portal display name.

        Returns:
            The newly created Student.
        """
        student = Student(
            tenant_id=tenant_id,
            external_id=external_id,
            display_name=display_name,
        )
        self._session.add(student)
        await self._session.flush()
        return student

    async def set_display_name(
        self,
        student_id: uuid.UUID,
        display_name: str | None,
    ) -> None:
        """Set a student's portal display_name (existing-Student provisioning)."""
        stmt = (
            update(Student)
            .where(Student.id == student_id)
            .values(display_name=display_name)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def get_by_id(self, student_id: uuid.UUID) -> Student | None:
        """Get a student by primary key."""
        return await self._session.get(Student, student_id)

    async def get_by_external_id(
        self,
        tenant_id: uuid.UUID,
        external_id: str,
    ) -> Student | None:
        """Get a student by tenant_id + external_id pair."""
        stmt = select(Student).where(
            Student.tenant_id == tenant_id,
            Student.external_id == external_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_for_tenant(self, tenant_id: uuid.UUID) -> list[Student]:
        """List all students for a tenant."""
        stmt = (
            select(Student)
            .where(Student.tenant_id == tenant_id)
            .order_by(Student.created_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
