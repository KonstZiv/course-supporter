"""Repository for StructureSnapshot CRUD operations."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from course_supporter.storage.orm import (
    GenerationMode,
    StructureSnapshot,
)


class SnapshotRepository:
    """Repository for structure snapshot operations.

    Tenant isolation is ensured at the API layer by verifying
    node ownership before accessing snapshots.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        node_id: uuid.UUID,
        node_fingerprint: str,
        mode: GenerationMode | str,
        structure: dict[str, Any],
        externalservicecall_id: uuid.UUID | None = None,
    ) -> StructureSnapshot:
        """Create a new snapshot record."""
        snapshot = StructureSnapshot(
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
        node_id: uuid.UUID,
        node_fingerprint: str,
        mode: GenerationMode | str,
    ) -> StructureSnapshot | None:
        """Find snapshot by the unique identity key.

        The identity is (node_id, node_fingerprint, mode).
        """
        stmt = select(StructureSnapshot).where(
            StructureSnapshot.node_id == node_id,
            StructureSnapshot.node_fingerprint == node_fingerprint,
            StructureSnapshot.mode == mode,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_for_node(
        self,
        node_id: uuid.UUID,
    ) -> StructureSnapshot | None:
        """Get the most recent snapshot for a specific node."""
        stmt = (
            select(StructureSnapshot)
            .options(joinedload(StructureSnapshot.service_call))
            .where(StructureSnapshot.node_id == node_id)
            .order_by(StructureSnapshot.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def count_for_node(self, node_id: uuid.UUID) -> int:
        """Count snapshots for a node (root or child)."""
        stmt = (
            select(func.count())
            .select_from(StructureSnapshot)
            .where(StructureSnapshot.node_id == node_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def list_for_node(
        self,
        node_id: uuid.UUID,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[StructureSnapshot]:
        """List snapshots for a node, newest first."""
        stmt = (
            select(StructureSnapshot)
            .options(joinedload(StructureSnapshot.service_call))
            .where(StructureSnapshot.node_id == node_id)
            .order_by(StructureSnapshot.created_at.desc())
        )
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
