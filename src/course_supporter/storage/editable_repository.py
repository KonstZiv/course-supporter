"""Repository for StructureNodeEditable CRUD operations."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.editable_conversion import (
    _CONTENT_FIELDS,
    convert_structure_nodes_to_editables,
)
from course_supporter.storage.orm import StructureNodeEditable
from course_supporter.storage.structure_node_repository import StructureNodeRepository


class EditableRepository:
    """Manage StructureNodeEditable persistence for a MaterialNode."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Create ──────────────────────────────────────────────

    async def create_tree(
        self,
        nodes: list[StructureNodeEditable],
    ) -> list[StructureNodeEditable]:
        """Bulk-insert a tree of editable nodes (parents before children)."""
        self._session.add_all(nodes)
        await self._session.flush()
        return nodes

    # ── Read ────────────────────────────────────────────────

    async def get_tree(
        self,
        materialnode_id: uuid.UUID,
    ) -> list[StructureNodeEditable]:
        """Load all editable nodes for a MaterialNode, ordered parents-first."""
        stmt = (
            select(StructureNodeEditable)
            .where(StructureNodeEditable.materialnode_id == materialnode_id)
            .order_by(
                StructureNodeEditable.parent_editable_id.is_(None).desc(),
                StructureNodeEditable.parent_editable_id,
                StructureNodeEditable.order,
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(
        self,
        editable_id: uuid.UUID,
    ) -> StructureNodeEditable | None:
        """Get a single editable node by primary key."""
        return await self._session.get(StructureNodeEditable, editable_id)

    # ── Update ──────────────────────────────────────────────

    async def update_fields(
        self,
        editable_id: uuid.UUID,
        fields: dict[str, Any],
    ) -> StructureNodeEditable:
        """Patch specific fields and auto-track in ``edited_fields``.

        Raises:
            ValueError: If the editable node is not found.
        """
        node = await self._session.get(StructureNodeEditable, editable_id)
        if node is None:
            msg = f"StructureNodeEditable {editable_id} not found"
            raise ValueError(msg)

        current_edited: list[str] = list(node.edited_fields or [])

        for key, value in fields.items():
            if key in _CONTENT_FIELDS:
                setattr(node, key, value)
                if key not in current_edited:
                    current_edited.append(key)

        node.edited_fields = current_edited
        await self._session.flush()
        return node

    # ── Delete ──────────────────────────────────────────────

    async def delete_tree(
        self,
        materialnode_id: uuid.UUID,
    ) -> int:
        """Delete all editable nodes for a MaterialNode. Returns count."""
        stmt = (
            delete(StructureNodeEditable)
            .where(StructureNodeEditable.materialnode_id == materialnode_id)
            .returning(func.count())
        )
        result = await self._session.execute(stmt)
        row = result.scalar()
        return int(row) if row else 0

    # ── Init from snapshot ──────────────────────────────────

    async def init_from_snapshot(
        self,
        snapshot_id: uuid.UUID,
        materialnode_id: uuid.UUID,
        *,
        preserve_edited: bool = False,
    ) -> list[StructureNodeEditable]:
        """Copy StructureNode tree from a snapshot into editable tree.

        If ``preserve_edited`` is True, manually edited field values
        from the existing editable tree are restored after re-init
        (matched by ``source_structurenode_id``).

        Args:
            snapshot_id: Source StructureSnapshot to copy from.
            materialnode_id: Target MaterialNode.
            preserve_edited: Whether to carry over manual edits.

        Returns:
            Newly created editable nodes.
        """
        # 1. Optionally save existing manual edits
        preserved: dict[uuid.UUID, dict[str, Any]] = {}
        if preserve_edited:
            old_editables = await self.get_tree(materialnode_id)
            for ed in old_editables:
                if ed.edited_fields and ed.source_structurenode_id is not None:
                    saved_values: dict[str, Any] = {}
                    for field_name in ed.edited_fields:
                        saved_values[field_name] = getattr(ed, field_name, None)
                    preserved[ed.source_structurenode_id] = {
                        "values": saved_values,
                        "edited_fields": list(ed.edited_fields),
                    }

        # 2. Delete existing editable tree
        await self.delete_tree(materialnode_id)

        # 3. Load StructureNodes from snapshot
        sn_repo = StructureNodeRepository(self._session)
        structure_nodes = await sn_repo.get_tree(snapshot_id)
        if not structure_nodes:
            return []

        # 4. Convert to editables
        editables = convert_structure_nodes_to_editables(
            structure_nodes, materialnode_id, snapshot_id
        )

        # 5. Restore preserved edits
        if preserved:
            for ed in editables:
                if ed.source_structurenode_id in preserved:
                    entry = preserved[ed.source_structurenode_id]
                    for field_name, value in entry["values"].items():
                        setattr(ed, field_name, value)
                    ed.edited_fields = entry["edited_fields"]

        # 6. Persist
        return await self.create_tree(editables)
