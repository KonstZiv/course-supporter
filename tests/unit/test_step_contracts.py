"""Tests for step pipeline data contracts (S3-020a)."""

from __future__ import annotations

import uuid

import pytest

from course_supporter.models.course import CourseStructure
from course_supporter.models.step import (
    Correction,
    NodeSummary,
    StepInput,
    StepOutput,
    StepType,
)


class TestStepType:
    """StepType enum values."""

    def test_values(self) -> None:
        assert StepType.GENERATE == "generate"
        assert StepType.RECONCILE == "reconcile"
        assert StepType.REFINE == "refine"

    def test_from_string(self) -> None:
        assert StepType("generate") is StepType.GENERATE


class TestNodeSummary:
    """NodeSummary dataclass."""

    def test_create(self) -> None:
        ns = NodeSummary(
            node_id=uuid.uuid4(),
            title="Intro",
            summary="Covers basics",
            core_concepts=["variables"],
            mentioned_concepts=["functions"],
            structure_snapshot_id=uuid.uuid4(),
        )
        assert ns.title == "Intro"
        assert ns.core_concepts == ["variables"]

    def test_frozen(self) -> None:
        ns = NodeSummary(
            node_id=uuid.uuid4(),
            title="T",
            summary="S",
            core_concepts=[],
            mentioned_concepts=[],
            structure_snapshot_id=None,
        )
        with pytest.raises(AttributeError):
            ns.title = "other"  # type: ignore[misc]


class TestCorrection:
    """Correction dataclass."""

    def test_create(self) -> None:
        c = Correction(
            target_node_id=uuid.uuid4(),
            field="title",
            action="rename",
            old_value="old",
            new_value="new",
            reason="consistency",
        )
        assert c.action == "rename"


class TestStepInput:
    """StepInput dataclass."""

    def test_create_minimal(self) -> None:
        si = StepInput(
            node_id=uuid.uuid4(),
            step_type=StepType.GENERATE,
            materials=[],
            children_summaries=[],
            parent_context=None,
            sibling_summaries=[],
            existing_structure=None,
            mode="free",
            material_tree=[],
        )
        assert si.step_type == StepType.GENERATE
        assert si.parent_context is None

    def test_frozen(self) -> None:
        si = StepInput(
            node_id=uuid.uuid4(),
            step_type=StepType.GENERATE,
            materials=[],
            children_summaries=[],
            parent_context=None,
            sibling_summaries=[],
            existing_structure=None,
            mode="free",
            material_tree=[],
        )
        with pytest.raises(AttributeError):
            si.mode = "guided"  # type: ignore[misc]


class TestStepOutput:
    """StepOutput dataclass."""

    def test_create_generate(self) -> None:
        from unittest.mock import MagicMock

        structure = MagicMock(spec=CourseStructure)
        response = MagicMock()

        so = StepOutput(
            structure=structure,
            summary="Covers basics",
            core_concepts=["var"],
            mentioned_concepts=["func"],
            prompt_version="v1_free",
            response=response,
        )
        assert so.corrections is None
        assert so.terminology_map is None
        assert so.core_concepts == ["var"]

    def test_create_reconcile(self) -> None:
        from unittest.mock import MagicMock

        so = StepOutput(
            structure=MagicMock(spec=CourseStructure),
            summary="",
            core_concepts=[],
            mentioned_concepts=[],
            prompt_version="v1_reconcile",
            response=MagicMock(),
            corrections=[
                Correction(
                    target_node_id=uuid.uuid4(),
                    field="title",
                    action="rename",
                    old_value="a",
                    new_value="b",
                    reason="consistency",
                )
            ],
            terminology_map={"var": "variable"},
        )
        assert len(so.corrections) == 1
        assert so.terminology_map == {"var": "variable"}


class TestCourseStructureConcepts:
    """CourseStructure extended fields and validator."""

    def test_defaults(self) -> None:
        cs = CourseStructure(title="Test")
        assert cs.summary == ""
        assert cs.core_concepts == []
        assert cs.mentioned_concepts == []

    def test_valid_disjoint(self) -> None:
        cs = CourseStructure(
            title="Test",
            core_concepts=["a", "b"],
            mentioned_concepts=["c", "d"],
        )
        assert cs.core_concepts == ["a", "b"]

    def test_overlap_raises(self) -> None:
        with pytest.raises(ValueError, match="must not overlap"):
            CourseStructure(
                title="Test",
                core_concepts=["a", "b"],
                mentioned_concepts=["b", "c"],
            )

    def test_empty_both_valid(self) -> None:
        cs = CourseStructure(
            title="Test",
            core_concepts=[],
            mentioned_concepts=[],
        )
        assert cs.core_concepts == []

    def test_backward_compatible_no_concepts(self) -> None:
        """Existing code that doesn't pass concepts still works."""
        cs = CourseStructure(
            title="Test",
            modules=[],
            expected_knowledge=["python"],
        )
        assert cs.summary == ""
        assert cs.core_concepts == []
