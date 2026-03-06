"""Tests for ReconcileAgent (S3-020c)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.agents.reconciler import (
    ReconcileAgent,
    _format_children_context,
    _format_parent_context,
    _format_sibling_context,
)
from course_supporter.llm.schemas import LLMResponse
from course_supporter.models.course import CourseStructure
from course_supporter.models.step import NodeSummary, StepInput, StepOutput, StepType

_MERGE_STEP = "course_supporter.ingestion.merge.MergeStep"


class TestReconcileAgentExecute:
    """ReconcileAgent.execute() bridges StepInput to LLM reconciliation."""

    @pytest.fixture(autouse=True)
    def _mock_merge(self) -> None:  # type: ignore[misc]
        ctx = MagicMock()
        ctx.documents = []
        ctx.model_dump_json.return_value = "{}"
        with patch(_MERGE_STEP) as merge_cls:
            merge_cls.return_value.merge.return_value = ctx
            yield

    async def test_returns_step_output(self) -> None:
        """execute() returns a StepOutput with reconciled structure."""
        structure = CourseStructure(
            title="Reconciled",
            summary="Reconciled course",
            core_concepts=["python"],
            mentioned_concepts=["java"],
        )
        response = LLMResponse(
            content="{}",
            provider="test",
            model_id="test-model",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.001,
        )

        router = MagicMock()
        router.complete_structured = AsyncMock(return_value=(structure, response))
        agent = ReconcileAgent(router, mode="free")

        step_input = StepInput(
            node_id=uuid.uuid4(),
            step_type=StepType.RECONCILE,
            materials=[],
            children_summaries=[],
            parent_context=None,
            sibling_summaries=[],
            existing_structure=None,
            mode="free",
            material_tree=[],
        )

        result = await agent.execute(step_input)

        assert isinstance(result, StepOutput)
        assert result.structure is structure
        assert result.summary == "Reconciled course"
        assert result.core_concepts == ["python"]
        assert result.mentioned_concepts == ["java"]
        assert result.prompt_version == "v1_reconcile"

    async def test_sliding_window_in_prompt(self) -> None:
        """execute() includes parent, sibling, children context in prompt."""
        structure = CourseStructure(title="T", summary="S")
        response = LLMResponse(
            content="{}",
            provider="test",
            model_id="test",
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
        )

        router = MagicMock()
        router.complete_structured = AsyncMock(return_value=(structure, response))
        agent = ReconcileAgent(router, mode="free")

        parent = NodeSummary(
            node_id=uuid.uuid4(),
            title="Parent",
            summary="Parent summary",
            core_concepts=["overview"],
            mentioned_concepts=[],
            structure_snapshot_id=uuid.uuid4(),
        )
        sibling = NodeSummary(
            node_id=uuid.uuid4(),
            title="Sibling",
            summary="Sibling summary",
            core_concepts=["arrays"],
            mentioned_concepts=[],
            structure_snapshot_id=uuid.uuid4(),
        )
        child = NodeSummary(
            node_id=uuid.uuid4(),
            title="Child",
            summary="Child summary",
            core_concepts=["loops"],
            mentioned_concepts=[],
            structure_snapshot_id=uuid.uuid4(),
        )

        step_input = StepInput(
            node_id=uuid.uuid4(),
            step_type=StepType.RECONCILE,
            materials=[],
            children_summaries=[child],
            parent_context=parent,
            sibling_summaries=[sibling],
            existing_structure=None,
            mode="free",
            material_tree=[],
        )

        await agent.execute(step_input)

        call_kwargs = router.complete_structured.call_args.kwargs
        prompt = call_kwargs["prompt"]
        assert "Parent summary" in prompt
        assert "Sibling summary" in prompt
        assert "Child summary" in prompt


class TestFormatParentContext:
    """_format_parent_context formats NodeSummary for reconciliation prompt."""

    def test_none_returns_empty(self) -> None:
        assert _format_parent_context(None) == ""

    def test_formats_parent(self) -> None:
        parent = NodeSummary(
            node_id=uuid.uuid4(),
            title="Root",
            summary="Course overview",
            core_concepts=["python", "basics"],
            mentioned_concepts=["advanced"],
            structure_snapshot_id=None,
        )
        result = _format_parent_context(parent)
        assert "## Parent Context" in result
        assert "Root" in result
        assert "Course overview" in result
        assert "python, basics" in result
        assert "advanced" in result


class TestFormatSiblingContext:
    """_format_sibling_context formats sibling NodeSummary list."""

    def test_empty_returns_empty(self) -> None:
        assert _format_sibling_context([]) == ""

    def test_formats_siblings(self) -> None:
        sib = NodeSummary(
            node_id=uuid.uuid4(),
            title="Part B",
            summary="Covers arrays",
            core_concepts=["arrays"],
            mentioned_concepts=[],
            structure_snapshot_id=None,
        )
        result = _format_sibling_context([sib])
        assert "## Sibling Summaries" in result
        assert "### Part B" in result
        assert "Covers arrays" in result
        assert "arrays" in result


class TestFormatChildrenContext:
    """_format_children_context formats children NodeSummary list."""

    def test_empty_returns_empty(self) -> None:
        assert _format_children_context([]) == ""

    def test_formats_children(self) -> None:
        child = NodeSummary(
            node_id=uuid.uuid4(),
            title="Subtopic",
            summary="Covers loops",
            core_concepts=["for", "while"],
            mentioned_concepts=["recursion"],
            structure_snapshot_id=None,
        )
        result = _format_children_context([child])
        assert "## Children Summaries" in result
        assert "### Subtopic" in result
        assert "Covers loops" in result
        assert "for, while" in result
        assert "recursion" in result
