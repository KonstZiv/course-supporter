"""Tests for StructureNode tree building and response schema."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from course_supporter.api.routes.generation import _flat_to_tree
from course_supporter.api.schemas import StructureNodeResponse


def _make_sn(
    *,
    node_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    node_type: str = "module",
    order: int = 0,
    title: str = "Node",
    **kwargs: object,
) -> SimpleNamespace:
    """Create a fake StructureNode-like object for testing."""
    defaults: dict[str, object] = {
        "id": node_id or uuid.uuid4(),
        "parent_structurenode_id": parent_id,
        "node_type": node_type,
        "order": order,
        "title": title,
        "description": None,
        "learning_goal": None,
        "expected_knowledge": None,
        "expected_skills": None,
        "prerequisites": None,
        "difficulty": None,
        "estimated_duration": None,
        "success_criteria": None,
        "assessment_method": None,
        "competencies": None,
        "key_concepts": None,
        "common_mistakes": None,
        "teaching_strategy": None,
        "activities": None,
        "children": [],
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestFlatToTree:
    def test_empty_list(self) -> None:
        result = _flat_to_tree([])
        assert result == []

    def test_single_root(self) -> None:
        node = _make_sn(title="Root")
        result = _flat_to_tree([node])
        assert len(result) == 1
        assert result[0].title == "Root"
        assert result[0].children == []

    def test_parent_child(self) -> None:
        parent_id = uuid.uuid4()
        parent = _make_sn(node_id=parent_id, title="Parent", node_type="module")
        child = _make_sn(
            parent_id=parent_id, title="Child", node_type="lesson", order=0
        )
        result = _flat_to_tree([parent, child])
        assert len(result) == 1
        assert result[0].title == "Parent"
        assert len(result[0].children) == 1
        assert result[0].children[0].title == "Child"

    def test_three_level_tree(self) -> None:
        mod_id = uuid.uuid4()
        les_id = uuid.uuid4()
        mod = _make_sn(node_id=mod_id, title="Module", node_type="module")
        les = _make_sn(
            node_id=les_id, parent_id=mod_id, title="Lesson", node_type="lesson"
        )
        con = _make_sn(parent_id=les_id, title="Concept", node_type="concept")
        result = _flat_to_tree([mod, les, con])
        assert len(result) == 1
        assert len(result[0].children) == 1
        assert len(result[0].children[0].children) == 1
        assert result[0].children[0].children[0].title == "Concept"

    def test_multiple_roots(self) -> None:
        r1 = _make_sn(title="Root 1")
        r2 = _make_sn(title="Root 2")
        result = _flat_to_tree([r1, r2])
        assert len(result) == 2

    def test_children_preserve_order(self) -> None:
        parent_id = uuid.uuid4()
        parent = _make_sn(node_id=parent_id, title="P")
        c1 = _make_sn(parent_id=parent_id, title="C1", order=0)
        c2 = _make_sn(parent_id=parent_id, title="C2", order=1)
        c3 = _make_sn(parent_id=parent_id, title="C3", order=2)
        result = _flat_to_tree([parent, c1, c2, c3])
        assert len(result[0].children) == 3
        assert [c.title for c in result[0].children] == ["C1", "C2", "C3"]

    def test_orphan_node_ignored(self) -> None:
        """Node with non-existent parent is silently dropped."""
        orphan = _make_sn(parent_id=uuid.uuid4(), title="Orphan")
        result = _flat_to_tree([orphan])
        assert result == []


class TestStructureNodeResponse:
    def test_from_attributes(self) -> None:
        sn = _make_sn(
            title="Test",
            node_type="module",
            description="Desc",
            difficulty="easy",
        )
        resp = StructureNodeResponse.model_validate(sn)
        assert resp.title == "Test"
        assert resp.node_type == "module"
        assert resp.description == "Desc"
        assert resp.difficulty == "easy"
        assert resp.children == []

    def test_recursive_serialization(self) -> None:
        resp = StructureNodeResponse(
            id=uuid.uuid4(),
            node_type="module",
            order=0,
            title="M1",
            children=[
                StructureNodeResponse(
                    id=uuid.uuid4(),
                    node_type="lesson",
                    order=0,
                    title="L1",
                )
            ],
        )
        data = resp.model_dump()
        assert len(data["children"]) == 1
        assert data["children"][0]["title"] == "L1"
        assert data["children"][0]["children"] == []

    def test_jsonb_fields(self) -> None:
        sn = _make_sn(
            title="M",
            expected_knowledge=[{"summary": "Python", "details": ""}],
            key_concepts=[{"summary": "OOP", "details": "Object-Oriented"}],
        )
        resp = StructureNodeResponse.model_validate(sn)
        assert resp.expected_knowledge == [{"summary": "Python", "details": ""}]
        assert resp.key_concepts == [{"summary": "OOP", "details": "Object-Oriented"}]
