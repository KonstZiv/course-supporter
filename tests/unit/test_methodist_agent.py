"""Tests for MethodistAgent."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from course_supporter.agents.methodist import (
    MethodistAgent,
    MethodistResult,
    format_material_roles,
    format_methodist_children,
    format_methodist_parent,
    format_methodist_siblings,
)
from course_supporter.agents.prompt_loader import PromptData
from course_supporter.llm.router import AllModelsFailedError
from course_supporter.llm.schemas import LLMResponse
from course_supporter.models.methodist import MethodistNodeOutput


@pytest.fixture()
def mock_router() -> AsyncMock:
    return AsyncMock()


def _sample_output() -> MethodistNodeOutput:
    return MethodistNodeOutput(
        learning_objectives=["Understand ORM basics"],
        teaching_recommendations="Use live coding demo.",
        rendered_markdown="# ORM\n\nContent.",
    )


def _sample_response() -> LLMResponse:
    return LLMResponse(
        content="{}",
        provider="deepseek",
        model_id="deepseek-chat",
        tokens_in=500,
        tokens_out=1000,
        latency_ms=2000,
    )


def _sample_prompt_data() -> PromptData:
    return PromptData(
        version="v1_methodist_leaf",
        system_prompt="You are a methodist.",
        user_prompt_template="Node: {node_title}\n{context}",
    )


class TestMethodistAgentInit:
    def test_default_params(self, mock_router: AsyncMock) -> None:
        agent = MethodistAgent(mock_router)
        assert agent._strategy == "default"
        assert agent._temperature == 0.0
        assert agent._max_tokens is None

    def test_custom_params(self, mock_router: AsyncMock) -> None:
        agent = MethodistAgent(
            mock_router,
            strategy="quality",
            temperature=0.3,
            max_tokens=8000,
        )
        assert agent._strategy == "quality"
        assert agent._temperature == 0.3
        assert agent._max_tokens == 8000


class TestMethodistAgentRun:
    async def test_run_returns_output(
        self,
        mock_router: AsyncMock,
    ) -> None:
        mock_router.complete_structured = AsyncMock(
            return_value=(_sample_output(), _sample_response()),
        )
        agent = MethodistAgent(mock_router)

        with patch(
            "course_supporter.agents.methodist.load_split_prompt",
            return_value=_sample_prompt_data(),
        ):
            output = await agent.run(
                node_title="Django Models",
                node_description="Introduction to models.",
                node_type="lesson",
                node_position="leaf",
                outline_context='{"title": "Models"}',
                structure_context='{"modules": []}',
            )

        assert isinstance(output, MethodistNodeOutput)
        assert output.learning_objectives == ["Understand ORM basics"]

    async def test_run_with_metadata(
        self,
        mock_router: AsyncMock,
    ) -> None:
        mock_router.complete_structured = AsyncMock(
            return_value=(_sample_output(), _sample_response()),
        )
        agent = MethodistAgent(mock_router)

        with patch(
            "course_supporter.agents.methodist.load_split_prompt",
            return_value=_sample_prompt_data(),
        ):
            result = await agent.run_with_metadata(
                node_title="Django Models",
                node_description="Introduction to models.",
                node_type="lesson",
                node_position="leaf",
                outline_context='{"title": "Models"}',
                structure_context='{"modules": []}',
            )

        assert isinstance(result, MethodistResult)
        assert result.prompt_version == "v1_methodist_leaf"
        assert result.response.model_id == "deepseek-chat"


class TestFormatHelpers:
    def test_format_children_empty(self) -> None:
        assert format_methodist_children([]) == ""

    def test_format_siblings_empty(self) -> None:
        assert format_methodist_siblings([]) == ""

    def test_format_parent_none(self) -> None:
        assert format_methodist_parent(None) == ""

    def test_format_material_roles_empty(self) -> None:
        assert format_material_roles([]) == ""

    def test_format_material_roles(self) -> None:
        entries = [
            ("Lecture 1", "video", "educational"),
            ("Syllabus", "text", "methodological"),
        ]
        result = format_material_roles(entries)
        assert "educational" in result
        assert "methodological" in result
        assert "Lecture 1" in result


class TestMethodistAgentErrorHandling:
    """Verify that LLM API errors propagate correctly."""

    async def test_all_models_failed_propagates(
        self,
        mock_router: AsyncMock,
    ) -> None:
        """AllModelsFailedError from ModelRouter propagates to caller."""
        mock_router.complete_structured = AsyncMock(
            side_effect=AllModelsFailedError(
                "methodist",
                ["default"],
                [("deepseek-chat", "rate limit exceeded")],
            ),
        )
        agent = MethodistAgent(mock_router)

        with (
            patch(
                "course_supporter.agents.methodist.load_split_prompt",
                return_value=_sample_prompt_data(),
            ),
            pytest.raises(AllModelsFailedError, match="methodist"),
        ):
            await agent.run(
                node_title="Node",
                node_description="Desc",
                node_type="lesson",
                node_position="leaf",
                outline_context="{}",
                structure_context="{}",
            )

    async def test_network_error_propagates(
        self,
        mock_router: AsyncMock,
    ) -> None:
        """Network/connection errors propagate to caller."""
        mock_router.complete_structured = AsyncMock(
            side_effect=ConnectionError("network unreachable"),
        )
        agent = MethodistAgent(mock_router)

        with (
            patch(
                "course_supporter.agents.methodist.load_split_prompt",
                return_value=_sample_prompt_data(),
            ),
            pytest.raises(ConnectionError),
        ):
            await agent.run(
                node_title="Node",
                node_description="Desc",
                node_type="lesson",
                node_position="leaf",
                outline_context="{}",
                structure_context="{}",
            )


class TestMethodistPromptSanitization:
    """Verify that context data is passed through to prompts safely."""

    async def test_special_chars_in_context_do_not_break_prompt(
        self,
        mock_router: AsyncMock,
    ) -> None:
        """Context with braces, quotes, newlines doesn't break formatting."""
        mock_router.complete_structured = AsyncMock(
            return_value=(_sample_output(), _sample_response()),
        )
        agent = MethodistAgent(mock_router)

        malicious_context = (
            '{"title": "Test {injection}", '
            '"desc": "line1\\nline2", '
            '"quote": "it\'s a \\"test\\""}'
        )

        with patch(
            "course_supporter.agents.methodist.load_split_prompt",
            return_value=_sample_prompt_data(),
        ):
            output = await agent.run(
                node_title='Node with "quotes" & {braces}',
                node_description="Desc with\nnewlines",
                node_type="lesson",
                node_position="leaf",
                outline_context=malicious_context,
                structure_context="{}",
            )

        assert isinstance(output, MethodistNodeOutput)
        # Verify the router was called (prompt was constructed)
        mock_router.complete_structured.assert_called_once()

    async def test_prompt_contains_context(
        self,
        mock_router: AsyncMock,
    ) -> None:
        """Verify prepared prompt includes the provided context data."""
        agent = MethodistAgent(mock_router)
        prompt_data = _sample_prompt_data()

        with patch(
            "course_supporter.agents.methodist.load_split_prompt",
            return_value=prompt_data,
        ):
            prepared = agent._prepare_prompts(
                node_title="Django Models",
                node_description="Intro to models",
                node_type="lesson",
                node_position="leaf",
                outline_context='{"sections": []}',
                structure_context='{"modules": []}',
                parent_context="",
                sibling_context="",
                children_context="",
                material_roles="",
            )

        assert "Django Models" in prepared.user_prompt
        assert prepared.prompt_version == "v1_methodist_leaf"
