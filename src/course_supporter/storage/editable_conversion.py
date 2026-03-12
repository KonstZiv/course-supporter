"""Convert StructureNode ORM objects into StructureNodeEditable ORM objects."""

from __future__ import annotations

import uuid

from course_supporter.storage.orm import (
    StructureNode,
    StructureNodeEditable,
    _uuid7,
)

# Fields to copy from StructureNode → StructureNodeEditable.
_CONTENT_FIELDS: tuple[str, ...] = (
    "node_type",
    "order",
    "title",
    "description",
    "learning_goal",
    "expected_knowledge",
    "expected_skills",
    "prerequisites",
    "difficulty",
    "estimated_duration",
    "success_criteria",
    "assessment_method",
    "competencies",
    "key_concepts",
    "common_mistakes",
    "teaching_strategy",
    "activities",
    "teaching_style",
    "deep_dive_references",
    "content_version",
    "timecodes",
    "slide_references",
    "web_references",
)


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
