"""Convert StructureNode ORM objects into StructureNodeEditable ORM objects."""

from __future__ import annotations

import uuid

from sqlalchemy import inspect as sa_inspect

from course_supporter.storage.orm import (
    StructureNode,
    StructureNodeEditable,
    _uuid7,
)

# Metadata-only columns excluded from content copying.
_EXCLUDED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "materialnode_id",
        "source_snapshot_id",
        "source_structurenode_id",
        "parent_editable_id",
        "edited_fields",
        "created_at",
        "updated_at",
    }
)


def _derive_content_fields() -> tuple[str, ...]:
    """Derive shared content columns from StructureNodeEditable mapper.

    Automatically picks up any new content columns added to the ORM
    model, avoiding brittle manual enumeration.
    """
    mapper = sa_inspect(StructureNodeEditable)
    return tuple(col.key for col in mapper.columns if col.key not in _EXCLUDED_FIELDS)


_CONTENT_FIELDS: tuple[str, ...] = _derive_content_fields()


def convert_structure_nodes_to_editables(
    structure_nodes: list[StructureNode],
    materialnode_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> list[StructureNodeEditable]:
    """Convert a flat StructureNode list to StructureNodeEditable list.

    The input list must be sorted parents-first (the default from
    ``StructureNodeRepository.get_tree``).  Parent-child relationships
    are remapped via an ID translation table.

    Args:
        structure_nodes: Flat list of StructureNode ORM objects.
        materialnode_id: Target MaterialNode to link editables to.
        snapshot_id: Source snapshot for provenance tracking.

    Returns:
        Flat list of StructureNodeEditable objects, parents before children.
    """
    id_map: dict[uuid.UUID, uuid.UUID] = {}  # old SN id → new editable id
    editables: list[StructureNodeEditable] = []

    for sn in structure_nodes:
        new_id = _uuid7()
        id_map[sn.id] = new_id

        parent_editable_id: uuid.UUID | None = None
        if sn.parent_structurenode_id is not None:
            parent_editable_id = id_map.get(sn.parent_structurenode_id)

        kwargs: dict[str, object] = {
            field: getattr(sn, field) for field in _CONTENT_FIELDS
        }

        editables.append(
            StructureNodeEditable(
                id=new_id,
                materialnode_id=materialnode_id,
                source_snapshot_id=snapshot_id,
                source_structurenode_id=sn.id,
                parent_editable_id=parent_editable_id,
                edited_fields=[],
                **kwargs,
            )
        )

    return editables
