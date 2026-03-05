"""Repository for MaterialNode CRUD and tree operations."""

from __future__ import annotations

import uuid
from enum import Enum, auto

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from course_supporter.storage.orm import MaterialNode

# Lazy import helper to avoid circular dependency at module load time.
# FingerprintService → orm.py ← MaterialNodeRepository → FingerprintService
# Actual import deferred to _invalidate_node_chain().


class _Unset(Enum):
    """Sentinel for distinguishing 'not provided' from None."""

    TOKEN = auto()


class MaterialNodeRepository:
    """Repository for material tree node operations.

    Root nodes (parent_materialnode_id IS NULL) represent "courses" — top-level
    entities owned by a tenant. Tenant isolation is enforced at the
    API layer via ``node.tenant_id``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Root node (= course) operations ──

    async def list_roots(
        self,
        tenant_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MaterialNode]:
        """List root nodes for a tenant, ordered newest first."""
        stmt = (
            select(MaterialNode)
            .where(
                MaterialNode.tenant_id == tenant_id,
                MaterialNode.parent_materialnode_id.is_(None),
            )
            .order_by(MaterialNode.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_roots(self, tenant_id: uuid.UUID) -> int:
        """Count root nodes for a tenant."""
        stmt = (
            select(func.count())
            .select_from(MaterialNode)
            .where(
                MaterialNode.tenant_id == tenant_id,
                MaterialNode.parent_materialnode_id.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    # ── CRUD ──

    async def create(
        self,
        *,
        tenant_id: uuid.UUID,
        title: str,
        parent_materialnode_id: uuid.UUID | None = None,
        description: str | None = None,
    ) -> MaterialNode:
        """Create a new node with auto-incremented order among siblings.

        Args:
            tenant_id: FK to the owning tenant.
            title: Node title.
            parent_materialnode_id: FK to parent node (None for root).
            description: Optional node description.

        Returns:
            The newly created MaterialNode.
        """
        next_order = await self._next_sibling_order(parent_materialnode_id)
        node = MaterialNode(
            tenant_id=tenant_id,
            parent_materialnode_id=parent_materialnode_id,
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

    async def get_roots(self, tenant_id: uuid.UUID) -> list[MaterialNode]:
        """Get root nodes (parent_materialnode_id=None) for a tenant, ordered."""
        stmt = (
            select(MaterialNode)
            .where(
                MaterialNode.tenant_id == tenant_id,
                MaterialNode.parent_materialnode_id.is_(None),
            )
            .order_by(MaterialNode.order)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_children(self, node_id: uuid.UUID) -> list[MaterialNode]:
        """Get direct children of a node, ordered."""
        stmt = (
            select(MaterialNode)
            .where(MaterialNode.parent_materialnode_id == node_id)
            .order_by(MaterialNode.order)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # ── Tree operations ──

    async def get_subtree(
        self,
        root_id: uuid.UUID,
        *,
        include_materials: bool = False,
    ) -> list[MaterialNode]:
        """Load entire subtree rooted at *root_id* and return with children populated.

        Uses a recursive CTE to find all descendant node IDs, then loads
        full ORM objects in a single query. Tree assembly happens in Python.

        Args:
            root_id: UUID of the root node.
            include_materials: If True, eager-load ``MaterialEntry``
                relationships for each node.

        Returns:
            List containing the root node with ``children`` populated
            recursively. Returns empty list if root_id not found.
        """
        # Recursive CTE: start from root_id, walk down via parent_materialnode_id
        base = select(MaterialNode.id).where(MaterialNode.id == root_id)
        cte = base.cte(name="subtree", recursive=True)
        recursive = select(MaterialNode.id).join(
            cte, MaterialNode.parent_materialnode_id == cte.c.id
        )
        cte = cte.union_all(recursive)

        # Load full node objects
        stmt = (
            select(MaterialNode)
            .where(MaterialNode.id.in_(select(cte.c.id)))
            .order_by(MaterialNode.order)
        )
        if include_materials:
            stmt = stmt.options(selectinload(MaterialNode.materials))
        result = await self._session.execute(stmt)
        all_nodes = list(result.scalars().all())

        # Build lookup and assemble tree
        by_id: dict[uuid.UUID, MaterialNode] = {n.id: n for n in all_nodes}
        roots: list[MaterialNode] = []

        for node in all_nodes:
            if hasattr(node, "_sa_instance_state"):
                set_committed_value(node, "children", [])
            else:
                node.children = []

        for node in all_nodes:
            if (
                node.parent_materialnode_id is None
                or node.parent_materialnode_id not in by_id
            ):
                roots.append(node)
            else:
                parent = by_id.get(node.parent_materialnode_id)
                if parent is not None:
                    parent.children.append(node)

        return roots

    async def move(
        self,
        node_id: uuid.UUID,
        new_parent_materialnode_id: uuid.UUID | None,
    ) -> MaterialNode:
        """Move a node to a new parent with cycle detection.

        Args:
            node_id: UUID of the node to move.
            new_parent_materialnode_id: New parent (None to make root).

        Returns:
            Updated MaterialNode.

        Raises:
            ValueError: If node not found, or move would create a cycle.
        """
        node = await self.get_by_id(node_id)
        if node is None:
            msg = f"MaterialNode not found: {node_id}"
            raise ValueError(msg)

        if new_parent_materialnode_id is not None:
            if new_parent_materialnode_id == node_id:
                msg = "Cannot move node to be its own parent"
                raise ValueError(msg)

            # Walk up from new_parent to root checking for cycle
            if await self._is_descendant(
                ancestor_id=node_id, node_id=new_parent_materialnode_id
            ):
                msg = (
                    f"Cannot move node {node_id} under {new_parent_materialnode_id}: "
                    f"would create a cycle"
                )
                raise ValueError(msg)

        old_parent_materialnode_id = node.parent_materialnode_id
        node.parent_materialnode_id = new_parent_materialnode_id
        node.order = await self._next_sibling_order(new_parent_materialnode_id)
        await self._session.flush()
        # Invalidate both old and new parent chains
        await self._invalidate_node_chain(old_parent_materialnode_id)
        await self._invalidate_node_chain(new_parent_materialnode_id)
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
                MaterialNode.parent_materialnode_id == node.parent_materialnode_id,
                MaterialNode.tenant_id == node.tenant_id,
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
        """Delete a node (children cascade via ORM) and invalidate parent.

        Raises:
            ValueError: If node not found.
        """
        node = await self.get_by_id(node_id)
        if node is None:
            msg = f"MaterialNode not found: {node_id}"
            raise ValueError(msg)
        parent_materialnode_id = node.parent_materialnode_id
        await self._session.delete(node)
        await self._session.flush()
        await self._invalidate_node_chain(parent_materialnode_id)

    # ── Private helpers ──

    async def _invalidate_node_chain(self, node_id: uuid.UUID | None) -> None:
        """Invalidate fingerprints from node up to root."""
        if node_id is None:
            return
        from course_supporter.fingerprint import FingerprintService

        node = await self._session.get(MaterialNode, node_id)
        if node is not None:
            await FingerprintService(self._session).invalidate_up(node)

    async def _next_sibling_order(
        self,
        parent_materialnode_id: uuid.UUID | None,
    ) -> int:
        """Get next order value for siblings under the given parent."""
        # SQLAlchemy translates `column == None` to `IS NULL`
        stmt = select(func.coalesce(func.max(MaterialNode.order) + 1, 0)).where(
            MaterialNode.parent_materialnode_id == parent_materialnode_id,
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

            if current.parent_materialnode_id == ancestor_id:
                return True
            current_id = current.parent_materialnode_id

        return False
