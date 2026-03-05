"""Repository for StructureSnapshot CRUD operations."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from course_supporter.storage.orm import (
    NIL_UUID,
    GenerationMode,
    StructureSnapshot,
)


class SnapshotRepository:
    """Repository for structure snapshot operations.

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
        mode: GenerationMode | str,
        structure: dict[str, Any],
        externalservicecall_id: uuid.UUID | None = None,
    ) -> StructureSnapshot:
        """Create a new snapshot record."""
        snapshot = StructureSnapshot(
            course_id=course_id,
            node_id=node_id,
            node_fingerprint=node_fingerprint,
            mode=mode,
            structure=structure,
            externalservicecall_id=externalservicecall_id,
        )
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def get_by_id(self, snapshot_id: uuid.UUID) -> StructureSnapshot | None:
        """Get a snapshot by primary key, eager-loading the service call."""
        stmt = (
            select(StructureSnapshot)
            .options(joinedload(StructureSnapshot.service_call))
            .where(StructureSnapshot.id == snapshot_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_by_identity(
        self,
        *,
        course_id: uuid.UUID,
        node_id: uuid.UUID | None,
        node_fingerprint: str,
        mode: GenerationMode | str,
    ) -> StructureSnapshot | None:
        """Find snapshot by the unique identity key.

        The identity is (course_id, node_id, node_fingerprint, mode).
        ``node_id=None`` means course-level snapshot.
        """
        stmt = select(StructureSnapshot).where(
            StructureSnapshot.course_id == course_id,
            func.coalesce(StructureSnapshot.node_id, NIL_UUID) == (node_id or NIL_UUID),
            StructureSnapshot.node_fingerprint == node_fingerprint,
            StructureSnapshot.mode == mode,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_for_node(
        self,
        course_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> StructureSnapshot | None:
        """Get the most recent snapshot for a specific node."""
        stmt = (
            select(StructureSnapshot)
            .options(joinedload(StructureSnapshot.service_call))
            .where(
                StructureSnapshot.course_id == course_id,
                StructureSnapshot.node_id == node_id,
            )
            .order_by(StructureSnapshot.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_for_course(
        self,
        course_id: uuid.UUID,
    ) -> StructureSnapshot | None:
        """Get the most recent course-level snapshot (node_id IS NULL)."""
        stmt = (
            select(StructureSnapshot)
            .options(joinedload(StructureSnapshot.service_call))
            .where(
                StructureSnapshot.course_id == course_id,
                StructureSnapshot.node_id.is_(None),
            )
            .order_by(StructureSnapshot.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def count_for_course(self, course_id: uuid.UUID) -> int:
        """Count snapshots for a course."""
        stmt = (
            select(func.count())
            .select_from(StructureSnapshot)
            .where(StructureSnapshot.course_id == course_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def list_for_course(
        self,
        course_id: uuid.UUID,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[StructureSnapshot]:
        """List snapshots for a course, newest first.

        Args:
            course_id: Course UUID.
            limit: Max rows to return (DB-level LIMIT).
            offset: Rows to skip (DB-level OFFSET).
        """
        stmt = (
            select(StructureSnapshot)
            .options(joinedload(StructureSnapshot.service_call))
            .where(StructureSnapshot.course_id == course_id)
            .order_by(StructureSnapshot.created_at.desc())
        )
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
