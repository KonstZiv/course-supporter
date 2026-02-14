"""CRUD repositories for database operations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import SourceMaterial

# Valid status transitions: current_status → set of allowed next statuses
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"processing"},
    "processing": {"done", "error"},
    "done": set(),  # terminal state
    # TODO: consider error → pending for retry workflow
    "error": set(),  # terminal state
}


class SourceMaterialRepository:
    """Repository for SourceMaterial CRUD operations.

    Encapsulates database access for source materials with
    status machine validation for processing lifecycle.

    Status machine::

        pending → processing → done
                             → error

    Invalid transitions raise ValueError.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        course_id: uuid.UUID,
        source_type: str,
        source_url: str,
        filename: str | None = None,
    ) -> SourceMaterial:
        """Create a new source material with status 'pending'.

        Args:
            course_id: FK to the parent course.
            source_type: One of 'video', 'presentation', 'text', 'web'.
            source_url: URL or path to the source file.
            filename: Optional original filename.

        Returns:
            The newly created SourceMaterial ORM instance.
        """
        material = SourceMaterial(
            course_id=course_id,
            source_type=source_type,
            source_url=source_url,
            filename=filename,
            status="pending",
        )
        self._session.add(material)
        await self._session.flush()
        return material

    async def get_by_id(self, material_id: uuid.UUID) -> SourceMaterial | None:
        """Get source material by its primary key.

        Args:
            material_id: UUID of the source material.

        Returns:
            SourceMaterial if found, None otherwise.
        """
        return await self._session.get(SourceMaterial, material_id)

    async def get_by_course_id(self, course_id: uuid.UUID) -> list[SourceMaterial]:
        """Get all source materials for a given course.

        Args:
            course_id: UUID of the parent course.

        Returns:
            List of SourceMaterial instances (may be empty).
        """
        stmt = (
            select(SourceMaterial)
            .where(SourceMaterial.course_id == course_id)
            .order_by(SourceMaterial.created_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        material_id: uuid.UUID,
        status: str,
        *,
        error_message: str | None = None,
        content_snapshot: str | None = None,
    ) -> SourceMaterial:
        """Update processing status with validation and side effects.

        Valid transitions:
            pending → processing
            processing → done (sets processed_at)
            processing → error (sets error_message)

        Args:
            material_id: UUID of the source material.
            status: New status value.
            error_message: Required when transitioning to 'error'.
            content_snapshot: Optional content snapshot to save.

        Returns:
            Updated SourceMaterial instance.

        Raises:
            ValueError: If material not found or transition is invalid.
        """
        material = await self.get_by_id(material_id)
        if material is None:
            raise ValueError(f"SourceMaterial not found: {material_id}")

        current_status = material.status
        allowed = VALID_TRANSITIONS.get(current_status, set())

        if status not in allowed:
            raise ValueError(
                f"Invalid status transition: '{current_status}' → '{status}'. "
                f"Allowed: {allowed or 'none (terminal state)'}"
            )

        material.status = status

        if status == "done":
            material.processed_at = datetime.now(UTC)

        if status == "error":
            if not error_message:
                raise ValueError(
                    "error_message is required when transitioning to 'error'"
                )
            material.error_message = error_message

        if content_snapshot is not None:
            material.content_snapshot = content_snapshot

        await self._session.flush()
        return material

    async def delete(self, material_id: uuid.UUID) -> None:
        """Delete a source material by ID.

        Args:
            material_id: UUID of the source material to delete.

        Raises:
            ValueError: If material not found.
        """
        material = await self.get_by_id(material_id)
        if material is None:
            raise ValueError(f"SourceMaterial not found: {material_id}")
        await self._session.delete(material)
        await self._session.flush()
