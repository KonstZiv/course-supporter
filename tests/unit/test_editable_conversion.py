"""Tests for editable_conversion (StructureNode → StructureNodeEditable).

Guards against the regression where columns that exist only on
``StructureNodeEditable`` (e.g. Methodist Layer 3 fields) leak into
``_CONTENT_FIELDS`` and break ``convert_structure_nodes_to_editables``
with ``AttributeError: 'StructureNode' object has no attribute ...``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import inspect as sa_inspect

from course_supporter.storage.editable_conversion import (
    _CONTENT_FIELDS,
    convert_structure_nodes_to_editables,
)
from course_supporter.storage.orm import StructureNode, StructureNodeEditable


def test_content_fields_are_present_on_structure_node() -> None:
    """Every field copied from SN must exist on SN.

    Regression guard: methodological_content/methodological_markdown/
    methodist_call_id live only on StructureNodeEditable and must NOT
    appear in _CONTENT_FIELDS.
    """
    sn_columns = {col.key for col in sa_inspect(StructureNode).columns}
    missing = set(_CONTENT_FIELDS) - sn_columns
    assert missing == set(), (
        f"_CONTENT_FIELDS references columns absent on StructureNode: "
        f"{sorted(missing)}. Add them to _EXCLUDED_FIELDS in "
        f"editable_conversion.py."
    )


def test_methodist_layer3_fields_excluded() -> None:
    """Methodist Layer 3 fields must be excluded from content copying."""
    excluded_from_copy = set(_CONTENT_FIELDS)
    for field in (
        "methodological_content",
        "methodological_markdown",
        "methodist_call_id",
    ):
        assert field not in excluded_from_copy, (
            f"{field} must not be copied from StructureNode; it is a "
            f"Methodist Layer 3 field populated later on the editable."
        )


def test_convert_tree_does_not_touch_editable_only_fields() -> None:
    """Smoke test: convert a minimal StructureNode without AttributeError."""
    sn = StructureNode(
        id=uuid.uuid4(),
        structuresnapshot_id=uuid.uuid4(),
        parent_structurenode_id=None,
        order=0,
        node_type="module",
        title="Module 1",
        description="Test module",
    )
    course_node_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    editables = convert_structure_nodes_to_editables(
        [sn],
        course_node_id=course_node_id,
        snapshot_id=snapshot_id,
    )

    assert len(editables) == 1
    ed = editables[0]
    assert isinstance(ed, StructureNodeEditable)
    assert ed.title == "Module 1"
    assert ed.node_type == "module"
    # Layer 3 fields remain unset on a freshly converted editable.
    assert ed.methodological_content is None
    assert ed.methodological_markdown is None
    assert ed.methodist_call_id is None
