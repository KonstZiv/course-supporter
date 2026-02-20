"""Repository for MaterialNode CRUD and tree operations."""

from __future__ import annotations

import uuid
from enum import Enum, auto

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import MaterialNode


class _Unset(Enum):
    """Sentinel for distinguishing 'not provided' from None."""

    TOKEN = auto()


class MaterialNodeRepository:
    """Repository for material tree node operations.

    Not tenant-scoped — tenant isolation is ensured at the API layer
    by verifying course ownership before accessing nodes.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        course_id: uuid.UUID,
        title: str,
        parent_id: uuid.UUID | None = None,
        description: str | None = None,
    ) -> MaterialNode:
        """Create a new node with auto-incremented order among siblings.

        Args:
            course_id: FK to the parent course.
            title: Node title.
            parent_id: FK to parent node (None for root).
            description: Optional node description.

        Returns:
            The newly created MaterialNode.
        """
        next_order = await self._next_sibling_order(course_id, parent_id)
        node = MaterialNode(
            course_id=course_id,
            parent_id=parent_id,
            title=title,
            description=description,
            order=next_order,
        )
        self._session.add(node)
        await self._session.flush()
        return node

    async def get_by_id(self, node_id: uuid.UUID) -> MaterialNode | None:
        """Get a node by primary key."""
        return await self._session.get(MaterialNode, node_id)

    async def get_roots(self, course_id: uuid.UUID) -> list[MaterialNode]:
        """Get root nodes (parent_id=None) for a course, ordered."""
        stmt = (
            select(MaterialNode)
            .where(
                MaterialNode.course_id == course_id,
                MaterialNode.parent_id.is_(None),
            )
            .order_by(MaterialNode.order)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_children(self, node_id: uuid.UUID) -> list[MaterialNode]:
        """Get direct children of a node, ordered."""
        stmt = (
            select(MaterialNode)
            .where(MaterialNode.parent_id == node_id)
            .order_by(MaterialNode.order)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_tree(self, course_id: uuid.UUID) -> list[MaterialNode]:
        """Load all nodes for a course and return root nodes with children populated.

        Loads all nodes in one query, then assembles the tree in Python.
        Children are sorted by ``order`` at each level.

        Returns:
            List of root MaterialNode instances with ``children`` populated.
        """
        stmt = (
            select(MaterialNode)
            .where(MaterialNode.course_id == course_id)
            .order_by(MaterialNode.order)
        )
        result = await self._session.execute(stmt)
        all_nodes = list(result.scalars().all())

        # Build lookup and assemble tree
        by_id: dict[uuid.UUID, MaterialNode] = {n.id: n for n in all_nodes}
        roots: list[MaterialNode] = []

        for node in all_nodes:
            # Reset ORM relationship collection before manual assembly
            # to prevent duplicates from lazy-loaded children.
            node.children = []

        for node in all_nodes:
            if node.parent_id is None:
                roots.append(node)
            else:
                parent = by_id.get(node.parent_id)
                if parent is not None:
                    parent.children.append(node)

        return roots

    async def move(
        self,
        node_id: uuid.UUID,
        new_parent_id: uuid.UUID | None,
    ) -> MaterialNode:
        """Move a node to a new parent with cycle detection.

        Args:
            node_id: UUID of the node to move.
            new_parent_id: New parent (None to make root).

        Returns:
            Updated MaterialNode.

        Raises:
            ValueError: If node not found, or move would create a cycle.
        """
        node = await self.get_by_id(node_id)
        if node is None:
            msg = f"MaterialNode not found: {node_id}"
            raise ValueError(msg)

        if new_parent_id is not None:
            if new_parent_id == node_id:
                msg = "Cannot move node to be its own parent"
                raise ValueError(msg)

            # Walk up from new_parent to root checking for cycle
            if await self._is_descendant(ancestor_id=node_id, node_id=new_parent_id):
                msg = (
                    f"Cannot move node {node_id} under {new_parent_id}: "
                    f"would create a cycle"
                )
                raise ValueError(msg)

        node.parent_id = new_parent_id
        node.order = await self._next_sibling_order(node.course_id, new_parent_id)
        await self._session.flush()
        return node

    async def reorder(self, node_id: uuid.UUID, new_order: int) -> MaterialNode:
        """Move a node to a new position among its siblings.

        Shifts other siblings to make room. Order values are
        renumbered 0, 1, 2, ... after the operation.

        Args:
            node_id: UUID of the node to reorder.
            new_order: Desired position (0-based).

        Returns:
            Updated MaterialNode.

        Raises:
            ValueError: If node not found or new_order is negative.
        """
        if new_order < 0:
            msg = f"new_order must be non-negative, got {new_order}"
            raise ValueError(msg)

        node = await self.get_by_id(node_id)
        if node is None:
            msg = f"MaterialNode not found: {node_id}"
            raise ValueError(msg)

        # Get siblings (including this node), ordered
        # SQLAlchemy translates `column == None` to `IS NULL`
        stmt = (
            select(MaterialNode)
            .where(
                MaterialNode.course_id == node.course_id,
                MaterialNode.parent_id == node.parent_id,
            )
            .order_by(MaterialNode.order)
        )
        result = await self._session.execute(stmt)
        siblings = list(result.scalars().all())

        # Remove node from current position
        siblings = [s for s in siblings if s.id != node_id]

        # Clamp new_order to valid range
        new_order = min(new_order, len(siblings))

        # Insert at new position
        siblings.insert(new_order, node)

        # Renumber all siblings
        for idx, sibling in enumerate(siblings):
            if sibling.order != idx:
                sibling.order = idx

        await self._session.flush()
        return node

    async def update(
        self,
        node_id: uuid.UUID,
        *,
        title: str | None = None,
        description: str | None | _Unset = _Unset.TOKEN,
    ) -> MaterialNode:
        """Update node fields.

        Args:
            node_id: UUID of the node.
            title: New title (if provided).
            description: New description (None to clear, _Unset to skip).

        Returns:
            Updated MaterialNode.

        Raises:
            ValueError: If node not found.
        """
        node = await self.get_by_id(node_id)
        if node is None:
            msg = f"MaterialNode not found: {node_id}"
            raise ValueError(msg)

        if title is not None:
            node.title = title
        if not isinstance(description, _Unset):
            node.description = description

        await self._session.flush()
        return node

    async def delete(self, node_id: uuid.UUID) -> None:
        """Delete a node (children cascade via ORM).

        Raises:
            ValueError: If node not found.
        """
        node = await self.get_by_id(node_id)
        if node is None:
            msg = f"MaterialNode not found: {node_id}"
            raise ValueError(msg)
        await self._session.delete(node)
        await self._session.flush()

    # ── Private helpers ──

    async def _next_sibling_order(
        self,
        course_id: uuid.UUID,
        parent_id: uuid.UUID | None,
    ) -> int:
        """Get next order value for siblings under the given parent."""
        # SQLAlchemy translates `column == None` to `IS NULL`
        stmt = select(func.coalesce(func.max(MaterialNode.order) + 1, 0)).where(
            MaterialNode.course_id == course_id,
            MaterialNode.parent_id == parent_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def _is_descendant(
        self,
        *,
        ancestor_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> bool:
        """Check if ancestor_id is an ancestor of node_id.

        Walks up from node_id to root. Returns True if ancestor_id
        is found on the path (would create a cycle if moved).
        """
        current_id: uuid.UUID | None = node_id
        visited: set[uuid.UUID] = set()

        while current_id is not None:
            if current_id in visited:
                break  # safety: existing cycle in data
            visited.add(current_id)

            current = await self.get_by_id(current_id)
            if current is None:
                break

            if current.parent_id == ancestor_id:
                return True
            current_id = current.parent_id

        return False
