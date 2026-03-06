"""Tests for RefineAgent (S3-020d)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.agents.refine import RefineAgent
from course_supporter.llm.schemas import LLMResponse
from course_supporter.models.course import CourseStructure
from course_supporter.models.step import NodeSummary, StepInput, StepOutput, StepType

_MERGE_STEP = "course_supporter.agents.refine.MergeStep"


class TestRefineAgentExecute:
    """RefineAgent.execute() preserves edits and harmonizes with context."""

    @pytest.fixture(autouse=True)
    def _mock_merge(self) -> None:  # type: ignore[misc]
        ctx = MagicMock()
        ctx.documents = []
        ctx.model_dump_json.return_value = "{}"
        with patch(_MERGE_STEP) as merge_cls:
            merge_cls.return_value.merge.return_value = ctx
            yield

    async def test_returns_step_output(self) -> None:
        """execute() returns a StepOutput with refined structure."""
        structure = CourseStructure(
            title="Refined",
            summary="Refined course",
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
        agent = RefineAgent(router, mode="free")

        step_input = StepInput(
            node_id=uuid.uuid4(),
            step_type=StepType.REFINE,
            materials=[],
            children_summaries=[],
            parent_context=None,
            sibling_summaries=[],
            existing_structure='{"modules": [{"title": "Edited"}]}',
            mode="free",
            material_tree=[],
        )

        result = await agent.execute(step_input)

        assert isinstance(result, StepOutput)
        assert result.structure is structure
        assert result.summary == "Refined course"
        assert result.prompt_version == "v1_refine"

    async def test_existing_structure_in_prompt(self) -> None:
        """execute() includes existing_structure in the prompt."""
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
        agent = RefineAgent(router, mode="free")

        edited = '{"modules": [{"title": "User Edited Module"}]}'
        step_input = StepInput(
            node_id=uuid.uuid4(),
            step_type=StepType.REFINE,
            materials=[],
            children_summaries=[],
            parent_context=None,
            sibling_summaries=[],
            existing_structure=edited,
            mode="free",
            material_tree=[],
        )

        await agent.execute(step_input)

        call_kwargs = router.complete_structured.call_args.kwargs
        assert "User Edited Module" in call_kwargs["prompt"]

    async def test_sliding_window_in_prompt(self) -> None:
        """execute() includes parent, sibling, children context."""
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
        agent = RefineAgent(router, mode="free")

        parent = NodeSummary(
            node_id=uuid.uuid4(),
            title="Parent",
            summary="Parent overview",
            core_concepts=[],
            mentioned_concepts=[],
            structure_snapshot_id=None,
        )
        sibling = NodeSummary(
            node_id=uuid.uuid4(),
            title="Sibling",
            summary="Sibling topic",
            core_concepts=[],
            mentioned_concepts=[],
            structure_snapshot_id=None,
        )

        step_input = StepInput(
            node_id=uuid.uuid4(),
            step_type=StepType.REFINE,
            materials=[],
            children_summaries=[],
            parent_context=parent,
            sibling_summaries=[sibling],
            existing_structure="{}",
            mode="free",
            material_tree=[],
        )

        await agent.execute(step_input)

        prompt = router.complete_structured.call_args.kwargs["prompt"]
        assert "Parent overview" in prompt
        assert "Sibling topic" in prompt

    async def test_no_existing_structure_fallback(self) -> None:
        """execute() handles None existing_structure gracefully."""
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
        agent = RefineAgent(router, mode="free")

        step_input = StepInput(
            node_id=uuid.uuid4(),
            step_type=StepType.REFINE,
            materials=[],
            children_summaries=[],
            parent_context=None,
            sibling_summaries=[],
            existing_structure=None,
            mode="free",
            material_tree=[],
        )

        await agent.execute(step_input)

        prompt = router.complete_structured.call_args.kwargs["prompt"]
        assert "No existing structure provided" in prompt
