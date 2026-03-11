"""Tests for ArchitectAgent.execute() — StepInput/StepOutput bridge (S3-020a)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.agents.architect import (
    ArchitectAgent,
    _format_children_context,
    _format_children_snapshots,
)
from course_supporter.llm.schemas import LLMResponse
from course_supporter.models.course import CourseContext, CourseStructure
from course_supporter.models.step import (
    ChildSnapshotContext,
    NodeSummary,
    StepInput,
    StepOutput,
    StepType,
)

_MERGE_STEP = "course_supporter.ingestion.merge.MergeStep"


class TestArchitectExecute:
    """ArchitectAgent.execute() bridges StepInput to run_with_metadata."""

    @pytest.fixture(autouse=True)
    def _mock_merge(self) -> None:  # type: ignore[misc]
        ctx = MagicMock(spec=CourseContext)
        ctx.documents = []
        with patch(_MERGE_STEP) as merge_cls:
            merge_cls.return_value.merge.return_value = ctx
            yield

    async def test_returns_step_output(self) -> None:
        """execute() returns a StepOutput with structure and metadata."""
        router = MagicMock()
        agent = ArchitectAgent(router, mode="free")

        structure = CourseStructure(
            title="Test Course",
            summary="A test course",
            core_concepts=["python"],
            mentioned_concepts=["java"],
        )
        llm_response = LLMResponse(
            content="{}",
            provider="test",
            model_id="test-model",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.001,
        )

        with patch.object(
            agent,
            "run_with_metadata",
            new_callable=AsyncMock,
        ) as mock_run:
            from course_supporter.agents.architect import GenerationResult

            mock_run.return_value = GenerationResult(
                structure=structure,
                prompt_version="v1_free",
                response=llm_response,
            )

            step_input = StepInput(
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

            result = await agent.execute(step_input)

        assert isinstance(result, StepOutput)
        assert result.structure is structure
        assert result.summary == "A test course"
        assert result.core_concepts == ["python"]
        assert result.mentioned_concepts == ["java"]
        assert result.prompt_version == "v1_free"
        assert result.response is llm_response

    async def test_passes_existing_structure(self) -> None:
        """execute() forwards existing_structure to run_with_metadata."""
        router = MagicMock()
        agent = ArchitectAgent(router, mode="guided")

        structure = CourseStructure(title="Test")
        llm_response = LLMResponse(
            content="{}",
            provider="test",
            model_id="test-model",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.001,
        )

        with patch.object(
            agent,
            "run_with_metadata",
            new_callable=AsyncMock,
        ) as mock_run:
            from course_supporter.agents.architect import GenerationResult

            mock_run.return_value = GenerationResult(
                structure=structure,
                prompt_version="v1_guided",
                response=llm_response,
            )

            step_input = StepInput(
                node_id=uuid.uuid4(),
                step_type=StepType.GENERATE,
                materials=[],
                children_summaries=[],
                parent_context=None,
                sibling_summaries=[],
                existing_structure='{"modules": []}',
                mode="guided",
                material_tree=[],
            )

            await agent.execute(step_input)

        mock_run.assert_awaited_once()
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["existing_structure"] == '{"modules": []}'

    async def test_corrections_default_none(self) -> None:
        """For generate steps, corrections and terminology_map are None."""
        router = MagicMock()
        agent = ArchitectAgent(router, mode="free")

        structure = CourseStructure(title="Test")
        llm_response = LLMResponse(
            content="{}",
            provider="test",
            model_id="test-model",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.001,
        )

        with patch.object(
            agent,
            "run_with_metadata",
            new_callable=AsyncMock,
        ) as mock_run:
            from course_supporter.agents.architect import GenerationResult

            mock_run.return_value = GenerationResult(
                structure=structure,
                prompt_version="v1_free",
                response=llm_response,
            )

            step_input = StepInput(
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

            result = await agent.execute(step_input)

        assert result.corrections is None
        assert result.terminology_map is None

    async def test_children_summaries_forwarded(self) -> None:
        """execute() formats children summaries and passes to prompt."""
        router = MagicMock()
        agent = ArchitectAgent(router, mode="free")

        structure = CourseStructure(title="Test")
        llm_response = LLMResponse(
            content="{}",
            provider="test",
            model_id="test-model",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.001,
        )

        with patch.object(
            agent,
            "run_with_metadata",
            new_callable=AsyncMock,
        ) as mock_run:
            from course_supporter.agents.architect import GenerationResult

            mock_run.return_value = GenerationResult(
                structure=structure,
                prompt_version="v1_free",
                response=llm_response,
            )

            child_summary = NodeSummary(
                node_id=uuid.uuid4(),
                title="Child Topic",
                summary="Covers basics",
                core_concepts=["variables"],
                mentioned_concepts=["functions"],
                structure_snapshot_id=uuid.uuid4(),
            )

            step_input = StepInput(
                node_id=uuid.uuid4(),
                step_type=StepType.GENERATE,
                materials=[],
                children_summaries=[child_summary],
                parent_context=None,
                sibling_summaries=[],
                existing_structure=None,
                mode="free",
                material_tree=[],
            )

            await agent.execute(step_input)

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["children_context"] is not None
        assert "Child Topic" in call_kwargs["children_context"]
        assert "Covers basics" in call_kwargs["children_context"]


class TestFormatChildrenContext:
    """_format_children_context formats NodeSummary list for prompts."""

    def test_empty_returns_empty_string(self) -> None:
        assert _format_children_context([]) == ""

    def test_single_child(self) -> None:
        cs = NodeSummary(
            node_id=uuid.uuid4(),
            title="Intro",
            summary="Introduction to Python",
            core_concepts=["python", "setup"],
            mentioned_concepts=["variables"],
            structure_snapshot_id=None,
        )
        result = _format_children_context([cs])
        assert result is not None
        assert "## Children Summaries" in result
        assert "### Intro" in result
        assert "Introduction to Python" in result
        assert "python, setup" in result
        assert "variables" in result

    def test_multiple_children(self) -> None:
        cs1 = NodeSummary(
            node_id=uuid.uuid4(),
            title="Part A",
            summary="First part",
            core_concepts=["a"],
            mentioned_concepts=[],
            structure_snapshot_id=None,
        )
        cs2 = NodeSummary(
            node_id=uuid.uuid4(),
            title="Part B",
            summary="Second part",
            core_concepts=[],
            mentioned_concepts=["b"],
            structure_snapshot_id=None,
        )
        result = _format_children_context([cs1, cs2])
        assert result is not None
        assert "### Part A" in result
        assert "### Part B" in result


class TestFormatChildrenSnapshots:
    """_format_children_snapshots formats ChildSnapshotContext for prompts."""

    def test_empty_returns_empty_string(self) -> None:
        assert _format_children_snapshots([]) == ""

    def test_single_snapshot_includes_structure(self) -> None:
        cs = ChildSnapshotContext(
            node_id=uuid.uuid4(),
            title="Conditionals",
            structure={"modules": [{"title": "If/Else"}]},
            summary="Covers conditional logic",
            core_concepts=["if", "else"],
            mentioned_concepts=["match"],
            summary_nested_nodes="",
        )
        result = _format_children_snapshots([cs])
        assert "## Child Node Snapshots" in result
        assert "### Conditionals" in result
        assert "Covers conditional logic" in result
        assert "if, else" in result
        assert '"If/Else"' in result  # structure JSON
        assert "Nested nodes summary" not in result  # empty → omitted

    def test_snapshot_with_nested_summary(self) -> None:
        cs = ChildSnapshotContext(
            node_id=uuid.uuid4(),
            title="Loops",
            structure={"modules": []},
            summary="Covers loops",
            core_concepts=["for", "while"],
            mentioned_concepts=[],
            summary_nested_nodes="Deep nested content about iteration patterns.",
        )
        result = _format_children_snapshots([cs])
        assert "**Nested nodes summary:** Deep nested content" in result

    def test_multiple_snapshots(self) -> None:
        cs1 = ChildSnapshotContext(
            node_id=uuid.uuid4(),
            title="Part A",
            structure={"title": "A"},
            summary="First",
            core_concepts=[],
            mentioned_concepts=[],
            summary_nested_nodes="",
        )
        cs2 = ChildSnapshotContext(
            node_id=uuid.uuid4(),
            title="Part B",
            structure={"title": "B"},
            summary="Second",
            core_concepts=[],
            mentioned_concepts=[],
            summary_nested_nodes="",
        )
        result = _format_children_snapshots([cs1, cs2])
        assert "### Part A" in result
        assert "### Part B" in result


class TestChildrenSnapshotsForwarding:
    """execute() forwards children_snapshots to run_with_metadata."""

    @pytest.fixture(autouse=True)
    def _mock_merge(self) -> None:  # type: ignore[misc]
        ctx = MagicMock(spec=CourseContext)
        ctx.documents = []
        with patch(_MERGE_STEP) as merge_cls:
            merge_cls.return_value.merge.return_value = ctx
            yield

    async def test_children_snapshots_formatted_and_passed(self) -> None:
        """execute() formats children_snapshots and passes to run_with_metadata."""
        router = MagicMock()
        agent = ArchitectAgent(router, mode="free")

        structure = CourseStructure(title="Test")
        llm_response = LLMResponse(
            content="{}",
            provider="test",
            model_id="test-model",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.001,
        )

        with patch.object(
            agent,
            "run_with_metadata",
            new_callable=AsyncMock,
        ) as mock_run:
            from course_supporter.agents.architect import GenerationResult

            mock_run.return_value = GenerationResult(
                structure=structure,
                prompt_version="v1_free",
                response=llm_response,
            )

            snap = ChildSnapshotContext(
                node_id=uuid.uuid4(),
                title="Child Module",
                structure={"modules": [{"title": "Sub"}]},
                summary="Child summary",
                core_concepts=["basics"],
                mentioned_concepts=[],
                summary_nested_nodes="Nested info",
            )

            step_input = StepInput(
                node_id=uuid.uuid4(),
                step_type=StepType.GENERATE,
                materials=[],
                children_summaries=[],
                parent_context=None,
                sibling_summaries=[],
                existing_structure=None,
                mode="free",
                material_tree=[],
                children_snapshots=[snap],
            )

            await agent.execute(step_input)

        call_kwargs = mock_run.call_args.kwargs
        assert "Child Module" in call_kwargs["children_snapshots"]
        assert "Nested info" in call_kwargs["children_snapshots"]

    async def test_empty_snapshots_passes_empty_string(self) -> None:
        """No children_snapshots → empty string passed."""
        router = MagicMock()
        agent = ArchitectAgent(router, mode="free")

        structure = CourseStructure(title="Test")
        llm_response = LLMResponse(
            content="{}",
            provider="test",
            model_id="test-model",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.001,
        )

        with patch.object(
            agent,
            "run_with_metadata",
            new_callable=AsyncMock,
        ) as mock_run:
            from course_supporter.agents.architect import GenerationResult

            mock_run.return_value = GenerationResult(
                structure=structure,
                prompt_version="v1_free",
                response=llm_response,
            )

            step_input = StepInput(
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

            await agent.execute(step_input)

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["children_snapshots"] == ""
