"""Repository for StructureNode CRUD operations."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from course_supporter.storage.orm import StructureNode


class StructureNodeRepository:
    """Manage StructureNode persistence within a snapshot."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_tree(
        self,
        nodes: list[StructureNode],
    ) -> list[StructureNode]:
        """Bulk-insert a tree of StructureNodes.

        Nodes must already have correct ``structuresnapshot_id``
        and ``parent_structurenode_id`` set. Order of insertion
        matters: parents before children.
        """
        self._session.add_all(nodes)
        await self._session.flush()
        return nodes

    async def get_tree(
        self,
        snapshot_id: uuid.UUID,
    ) -> list[StructureNode]:
        """Load all nodes for a snapshot, ordered by (parent, order).

        Returns a flat list; caller can build the tree using
        ``parent_structurenode_id``.
        """
        stmt = (
            select(StructureNode)
            .where(StructureNode.structuresnapshot_id == snapshot_id)
            .order_by(
                StructureNode.parent_structurenode_id.is_(None).desc(),
                StructureNode.parent_structurenode_id,
                StructureNode.order,
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_tree_eager(
        self,
        snapshot_id: uuid.UUID,
    ) -> list[StructureNode]:
        """Load root nodes with children eagerly loaded (2 levels)."""
        stmt = (
            select(StructureNode)
            .where(
                StructureNode.structuresnapshot_id == snapshot_id,
                StructureNode.parent_structurenode_id.is_(None),
            )
            .options(
                selectinload(StructureNode.children).selectinload(
                    StructureNode.children
                )
            )
            .order_by(StructureNode.order)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_type(
        self,
        snapshot_id: uuid.UUID,
        node_type: str,
    ) -> list[StructureNode]:
        """Filter nodes by type within a snapshot."""
        stmt = (
            select(StructureNode)
            .where(
                StructureNode.structuresnapshot_id == snapshot_id,
                StructureNode.node_type == node_type,
            )
            .order_by(StructureNode.order)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_for_snapshot(
        self,
        snapshot_id: uuid.UUID,
    ) -> int:
        """Count nodes in a snapshot."""
        from sqlalchemy import func

        stmt = select(func.count()).where(
            StructureNode.structuresnapshot_id == snapshot_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()
