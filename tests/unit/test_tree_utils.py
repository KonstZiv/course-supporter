"""Tests for tree_utils module."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from course_supporter.errors import NodeNotFoundError
from course_supporter.models.course import MaterialNodeSummary
from course_supporter.tree_utils import (
    build_material_tree_summary,
    resolve_target_nodes,
    serialize_tree_for_guided,
)

# ── Helpers ──


def _make_node(
    *,
    node_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    title: str = "Node",
    description: str | None = None,
    order: int = 0,
    children: list[Any] | None = None,
    materials: list[Any] | None = None,
) -> MagicMock:
    """Create a mock MaterialNode."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.parent_id = parent_id
    node.title = title
    node.description = description
    node.order = order
    node.children = children or []
    node.materials = materials or []
    return node


def _make_entry(
    *,
    state: str = "ready",
    filename: str | None = "notes.md",
    source_url: str = "file:///notes.md",
) -> MagicMock:
    """Create a mock MaterialEntry."""
    entry = MagicMock()
    entry.state = state
    entry.filename = filename
    entry.source_url = source_url
    return entry


# ── Tests ──


class TestBuildMaterialTreeSummary:
    def test_empty_flat_nodes(self) -> None:
        """Empty input produces empty output."""
        result = build_material_tree_summary([])
        assert result == []

    def test_single_root_no_materials(self) -> None:
        """Single root node with no materials."""
        root = _make_node(title="Root", order=1)
        result = build_material_tree_summary([root])

        assert len(result) == 1
        assert result[0].title == "Root"
        assert result[0].order == 1
        assert result[0].material_titles == []
        assert result[0].children == []

    def test_single_root_with_ready_materials(self) -> None:
        """Root node with READY materials collects filenames."""
        entry1 = _make_entry(filename="lecture.pdf")
        entry2 = _make_entry(filename="notes.md")
        root = _make_node(title="Module 1", materials=[entry1, entry2])

        result = build_material_tree_summary([root])

        assert result[0].material_titles == ["lecture.pdf", "notes.md"]

    def test_non_ready_materials_excluded(self) -> None:
        """Only READY materials are included."""
        ready = _make_entry(state="ready", filename="good.md")
        raw = _make_entry(state="raw", filename="raw.md")
        pending = _make_entry(state="pending", filename="pending.md")
        error = _make_entry(state="error", filename="error.md")
        root = _make_node(materials=[ready, raw, pending, error])

        result = build_material_tree_summary([root])

        assert result[0].material_titles == ["good.md"]

    def test_filename_none_falls_back_to_source_url(self) -> None:
        """When filename is None, source_url is used."""
        entry = _make_entry(filename=None, source_url="https://example.com/page")
        root = _make_node(materials=[entry])

        result = build_material_tree_summary([root])

        assert result[0].material_titles == ["https://example.com/page"]

    def test_nested_tree_structure(self) -> None:
        """Parent-child hierarchy is preserved."""
        child_id = uuid.uuid4()
        root_id = uuid.uuid4()

        child = _make_node(
            node_id=child_id,
            parent_id=root_id,
            title="Lesson 1",
            order=1,
            materials=[_make_entry(filename="video.mp4")],
        )
        root = _make_node(
            node_id=root_id,
            title="Module 1",
            order=0,
            children=[child],
            materials=[_make_entry(filename="overview.md")],
        )

        result = build_material_tree_summary([root, child])

        assert len(result) == 1
        assert result[0].title == "Module 1"
        assert result[0].material_titles == ["overview.md"]
        assert len(result[0].children) == 1
        assert result[0].children[0].title == "Lesson 1"
        assert result[0].children[0].material_titles == ["video.mp4"]

    def test_description_preserved(self) -> None:
        """Node description is included in summary."""
        root = _make_node(title="Module", description="Intro module")

        result = build_material_tree_summary([root])

        assert result[0].description == "Intro module"

    def test_multiple_roots(self) -> None:
        """Multiple root nodes produce multiple summaries."""
        root1 = _make_node(title="Module 1", order=0)
        root2 = _make_node(title="Module 2", order=1)

        result = build_material_tree_summary([root1, root2])

        assert len(result) == 2
        titles = [s.title for s in result]
        assert "Module 1" in titles
        assert "Module 2" in titles

    def test_deep_nesting(self) -> None:
        """Three-level nesting works correctly."""
        grandchild_id = uuid.uuid4()
        child_id = uuid.uuid4()
        root_id = uuid.uuid4()

        grandchild = _make_node(
            node_id=grandchild_id,
            parent_id=child_id,
            title="Concept",
            order=0,
        )
        child = _make_node(
            node_id=child_id,
            parent_id=root_id,
            title="Lesson",
            order=0,
            children=[grandchild],
        )
        root = _make_node(
            node_id=root_id,
            title="Module",
            order=0,
            children=[child],
        )

        result = build_material_tree_summary([root, child, grandchild])

        assert result[0].title == "Module"
        assert result[0].children[0].title == "Lesson"
        assert result[0].children[0].children[0].title == "Concept"

    def test_result_is_pydantic_model(self) -> None:
        """Result items are MaterialNodeSummary instances."""
        root = _make_node(title="Module")
        result = build_material_tree_summary([root])

        assert isinstance(result[0], MaterialNodeSummary)

    def test_serializable(self) -> None:
        """Result can be serialized to JSON (for CourseContext)."""
        entry = _make_entry(filename="test.pdf")
        root = _make_node(title="Module", materials=[entry])

        result = build_material_tree_summary([root])

        # Should not raise
        json_str = result[0].model_dump_json()
        assert "Module" in json_str
        assert "test.pdf" in json_str

    def test_children_outside_flat_nodes_excluded(self) -> None:
        """Children not in flat_nodes are excluded from the summary."""
        root_id = uuid.uuid4()
        child_in = _make_node(
            node_id=uuid.uuid4(),
            parent_id=root_id,
            title="Included",
            order=0,
        )
        child_out = _make_node(
            node_id=uuid.uuid4(),
            parent_id=root_id,
            title="Excluded",
            order=1,
        )
        root = _make_node(
            node_id=root_id,
            title="Root",
            order=0,
            children=[child_in, child_out],
        )

        # Only root and child_in are in flat_nodes
        result = build_material_tree_summary([root, child_in])

        assert len(result) == 1
        assert len(result[0].children) == 1
        assert result[0].children[0].title == "Included"

    def test_none_filename_and_none_source_url_skipped(self) -> None:
        """Entry with both filename=None and source_url=None is skipped."""
        entry = _make_entry(state="ready", filename=None, source_url="")
        root = _make_node(materials=[entry])

        result = build_material_tree_summary([root])

        assert result[0].material_titles == []

    def test_children_sorted_by_order(self) -> None:
        """Children are sorted by order regardless of ORM loading order."""
        root_id = uuid.uuid4()
        child_b = _make_node(
            node_id=uuid.uuid4(), parent_id=root_id, title="Second", order=2
        )
        child_a = _make_node(
            node_id=uuid.uuid4(), parent_id=root_id, title="First", order=1
        )
        root = _make_node(
            node_id=root_id,
            title="Root",
            order=0,
            # ORM may return in arbitrary order
            children=[child_b, child_a],
        )

        result = build_material_tree_summary([root, child_b, child_a])

        assert [c.title for c in result[0].children] == ["First", "Second"]


# ── resolve_target_nodes ──


class TestResolveTargetNodes:
    def test_course_level_returns_all_nodes(self) -> None:
        """node_id=None returns (None, flat list of all nodes)."""
        child = _make_node(title="Child")
        root = _make_node(title="Root", children=[child])

        target, flat = resolve_target_nodes([root], uuid.uuid4(), None)

        assert target is None
        assert len(flat) == 2
        assert root in flat
        assert child in flat

    def test_node_level_returns_target_and_subtree(self) -> None:
        """node_id=existing returns (target, subtree_flat)."""
        grandchild = _make_node(title="Grandchild")
        child_id = uuid.uuid4()
        child = _make_node(
            node_id=child_id,
            title="Child",
            children=[grandchild],
        )
        root = _make_node(title="Root", children=[child])

        target, flat = resolve_target_nodes([root], uuid.uuid4(), child_id)

        assert target is child
        assert len(flat) == 2
        assert child in flat
        assert grandchild in flat
        assert root not in flat

    def test_missing_node_raises(self) -> None:
        """node_id not found raises NodeNotFoundError."""
        root = _make_node(title="Root")

        with pytest.raises(NodeNotFoundError):
            resolve_target_nodes([root], uuid.uuid4(), uuid.uuid4())

    def test_multiple_roots_course_level(self) -> None:
        """Multiple roots with node_id=None flattens all."""
        root1 = _make_node(title="Root1")
        root2_child = _make_node(title="R2 Child")
        root2 = _make_node(title="Root2", children=[root2_child])

        _, flat = resolve_target_nodes(
            [root1, root2],
            uuid.uuid4(),
            None,
        )

        assert len(flat) == 3

    def test_empty_roots(self) -> None:
        """Empty root list with node_id=None returns empty flat list."""
        target, flat = resolve_target_nodes([], uuid.uuid4(), None)

        assert target is None
        assert flat == []


# ── serialize_tree_for_guided ──


class TestSerializeTreeForGuided:
    def test_single_root_valid_json(self) -> None:
        """Single root produces valid JSON with title, description, order."""
        root = _make_node(title="Module 1", description="Intro", order=0)

        result = serialize_tree_for_guided([root])
        parsed = json.loads(result)

        assert len(parsed) == 1
        assert parsed[0]["title"] == "Module 1"
        assert parsed[0]["description"] == "Intro"
        assert parsed[0]["order"] == 0

    def test_nested_tree_has_children_key(self) -> None:
        """Nested tree includes children key with child nodes."""
        root_id = uuid.uuid4()
        child = _make_node(
            title="Lesson 1",
            order=0,
            parent_id=root_id,
        )
        root = _make_node(
            node_id=root_id,
            title="Module 1",
            order=0,
            children=[child],
        )

        result = serialize_tree_for_guided([root, child])
        parsed = json.loads(result)

        assert len(parsed) == 1
        assert "children" in parsed[0]
        assert len(parsed[0]["children"]) == 1
        assert parsed[0]["children"][0]["title"] == "Lesson 1"

    def test_leaf_node_no_children_key(self) -> None:
        """Leaf node (no children) omits the 'children' key."""
        root = _make_node(title="Leaf", children=[])

        result = serialize_tree_for_guided([root])
        parsed = json.loads(result)

        assert "children" not in parsed[0]

    def test_multiple_roots(self) -> None:
        """Multiple root nodes produce a JSON list."""
        root1 = _make_node(title="Module 1", order=0)
        root2 = _make_node(title="Module 2", order=1)

        result = serialize_tree_for_guided([root1, root2])
        parsed = json.loads(result)

        assert len(parsed) == 2
        titles = {item["title"] for item in parsed}
        assert titles == {"Module 1", "Module 2"}

    def test_deep_nesting(self) -> None:
        """Three-level nesting serialized correctly."""
        gc_id = uuid.uuid4()
        c_id = uuid.uuid4()
        r_id = uuid.uuid4()

        grandchild = _make_node(
            node_id=gc_id,
            parent_id=c_id,
            title="Concept",
            order=0,
        )
        child = _make_node(
            node_id=c_id,
            parent_id=r_id,
            title="Lesson",
            order=0,
            children=[grandchild],
        )
        root = _make_node(
            node_id=r_id,
            title="Module",
            order=0,
            children=[child],
        )

        result = serialize_tree_for_guided([root, child, grandchild])
        parsed = json.loads(result)

        module = parsed[0]
        lesson = module["children"][0]
        concept = lesson["children"][0]
        assert concept["title"] == "Concept"
