"""Repository for CourseStructureSnapshot CRUD operations."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import CourseStructureSnapshot

_NIL_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


class SnapshotRepository:
    """Repository for course structure snapshot operations.

    Not tenant-scoped — tenant isolation is ensured at the API layer
    by verifying course ownership before accessing snapshots.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        course_id: uuid.UUID,
        node_id: uuid.UUID | None = None,
        node_fingerprint: str,
        mode: str,
        structure: dict[str, Any],
        prompt_version: str | None = None,
        model_id: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
    ) -> CourseStructureSnapshot:
        """Create a new snapshot record."""
        snapshot = CourseStructureSnapshot(
            course_id=course_id,
            node_id=node_id,
            node_fingerprint=node_fingerprint,
            mode=mode,
            structure=structure,
            prompt_version=prompt_version,
            model_id=model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def get_by_id(self, snapshot_id: uuid.UUID) -> CourseStructureSnapshot | None:
        """Get a snapshot by primary key."""
        return await self._session.get(CourseStructureSnapshot, snapshot_id)

    async def find_by_identity(
        self,
        *,
        course_id: uuid.UUID,
        node_id: uuid.UUID | None,
        node_fingerprint: str,
        mode: str,
    ) -> CourseStructureSnapshot | None:
        """Find snapshot by the unique identity key.

        The identity is (course_id, node_id, node_fingerprint, mode).
        ``node_id=None`` means course-level snapshot.
        """
        stmt = select(CourseStructureSnapshot).where(
            CourseStructureSnapshot.course_id == course_id,
            func.coalesce(CourseStructureSnapshot.node_id, _NIL_UUID)
            == (node_id or _NIL_UUID),
            CourseStructureSnapshot.node_fingerprint == node_fingerprint,
            CourseStructureSnapshot.mode == mode,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_for_node(
        self,
        course_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> CourseStructureSnapshot | None:
        """Get the most recent snapshot for a specific node."""
        stmt = (
            select(CourseStructureSnapshot)
            .where(
                CourseStructureSnapshot.course_id == course_id,
                CourseStructureSnapshot.node_id == node_id,
            )
            .order_by(CourseStructureSnapshot.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_for_course(
        self,
        course_id: uuid.UUID,
    ) -> CourseStructureSnapshot | None:
        """Get the most recent course-level snapshot (node_id IS NULL)."""
        stmt = (
            select(CourseStructureSnapshot)
            .where(
                CourseStructureSnapshot.course_id == course_id,
                CourseStructureSnapshot.node_id.is_(None),
            )
            .order_by(CourseStructureSnapshot.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_course(
        self,
        course_id: uuid.UUID,
    ) -> list[CourseStructureSnapshot]:
        """List all snapshots for a course, newest first."""
        stmt = (
            select(CourseStructureSnapshot)
            .where(CourseStructureSnapshot.course_id == course_id)
            .order_by(CourseStructureSnapshot.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
