"""Tests for OutlineAgent."""

from unittest.mock import AsyncMock, patch

import pytest

from course_supporter.agents.outline import (
    OUTLINE_PROMPT_PATH,
    OutlineAgent,
    OutlineResult,
    PreparedOutlinePrompt,
)
from course_supporter.agents.prompt_loader import PromptData
from course_supporter.llm.router import AllModelsFailedError
from course_supporter.llm.schemas import LLMResponse
from course_supporter.models.outline import (
    MaterialOutline,
    OutlineSection,
    PresenterInfo,
)
from course_supporter.models.source import SourceDocument


def _sample_outline() -> MaterialOutline:
    """Build a minimal MaterialOutline for mocking."""
    return MaterialOutline(
        title="Python Basics",
        duration_sec=960.0,
        language="uk",
        source_type="video",
        presenter=PresenterInfo(
            description="Senior dev",
            style="conversational",
            delivery_notes="Moderate pace.",
        ),
        summary="Intro to Python.",
        topics=["variables"],
        tools=["PyCharm"],
        target_audience="Beginners",
        prerequisites=[],
        sections=[
            OutlineSection(
                start_sec=0.0,
                end_sec=120.0,
                title="Introduction",
                narration="Welcome to the course.",
                screen_content="Title slide.",
                code_snippets=[],
                key_concepts=["Python"],
            ),
        ],
    )


def _sample_response() -> LLMResponse:
    return LLMResponse(
        content="{}",
        provider="gemini",
        model_id="gemini-2.5-flash",
        tokens_in=500,
        tokens_out=1000,
    )


@pytest.fixture()
def mock_router() -> AsyncMock:
    """ModelRouter mock with complete_structured returning MaterialOutline."""
    router = AsyncMock()
    router.complete_structured.return_value = (_sample_outline(), _sample_response())
    return router


@pytest.fixture()
def sample_source_doc() -> SourceDocument:
    """Minimal SourceDocument for testing."""
    return SourceDocument(
        source_type="video",
        source_url="file:///lecture.mp4",
        title="Python Basics",
    )


@pytest.fixture()
def prompt_data() -> PromptData:
    """Mock prompt data for outline."""
    return PromptData(
        version="v1_outline",
        system_prompt="You are an outline generator.",
        user_prompt_template="Source:\n{context}\nGenerate outline.",
    )


class TestOutlineAgentInit:
    def test_default_params(self, mock_router: AsyncMock) -> None:
        """OutlineAgent initializes with sensible defaults."""
        agent = OutlineAgent(mock_router)
        assert agent._strategy == "default"
        assert agent._temperature == 0.0
        assert agent._max_tokens is None
        assert agent._prompt_path == OUTLINE_PROMPT_PATH

    def test_custom_params(self, mock_router: AsyncMock) -> None:
        """OutlineAgent accepts custom parameters."""
        agent = OutlineAgent(
            mock_router,
            strategy="quality",
            temperature=0.3,
            max_tokens=8192,
            prompt_path="custom/outline.yaml",
        )
        assert agent._strategy == "quality"
        assert agent._temperature == 0.3
        assert agent._max_tokens == 8192
        assert agent._prompt_path == "custom/outline.yaml"


class TestPreparePrompt:
    """Tests for _prepare_prompt step (independent of LLM)."""

    def test_returns_prepared_prompt(
        self,
        mock_router: AsyncMock,
        sample_source_doc: SourceDocument,
        prompt_data: PromptData,
    ) -> None:
        """_prepare_prompt returns PreparedOutlinePrompt with correct fields."""
        with patch(
            "course_supporter.agents.outline.load_prompt",
            return_value=prompt_data,
        ):
            agent = OutlineAgent(mock_router)
            prepared = agent._prepare_prompt(sample_source_doc)

        assert isinstance(prepared, PreparedOutlinePrompt)
        assert prepared.system_prompt == "You are an outline generator."
        assert prepared.prompt_version == "v1_outline"

    def test_serializes_source_doc_into_prompt(
        self,
        mock_router: AsyncMock,
        sample_source_doc: SourceDocument,
        prompt_data: PromptData,
    ) -> None:
        """_prepare_prompt injects serialized SourceDocument into user prompt."""
        with patch(
            "course_supporter.agents.outline.load_prompt",
            return_value=prompt_data,
        ):
            agent = OutlineAgent(mock_router)
            prepared = agent._prepare_prompt(sample_source_doc)

        assert "Source:" in prepared.user_prompt
        assert "file:///lecture.mp4" in prepared.user_prompt
        assert "{context}" not in prepared.user_prompt

    def test_propagates_file_not_found(
        self,
        mock_router: AsyncMock,
        sample_source_doc: SourceDocument,
    ) -> None:
        """_prepare_prompt propagates FileNotFoundError."""
        agent = OutlineAgent(mock_router, prompt_path="nonexistent/prompt.yaml")
        with pytest.raises(FileNotFoundError):
            agent._prepare_prompt(sample_source_doc)


class TestGenerate:
    """Tests for _generate step (LLM interaction)."""

    @pytest.mark.asyncio
    async def test_returns_material_outline(self, mock_router: AsyncMock) -> None:
        """_generate returns (MaterialOutline, LLMResponse) tuple."""
        agent = OutlineAgent(mock_router)
        prepared = PreparedOutlinePrompt(
            system_prompt="System.",
            user_prompt="User prompt.",
            prompt_version="v1_outline",
        )
        outline, response = await agent._generate(prepared)

        assert isinstance(outline, MaterialOutline)
        assert outline.title == "Python Basics"
        assert len(outline.sections) == 1
        assert response.model_id == "gemini-2.5-flash"

    @pytest.mark.asyncio
    async def test_calls_router_with_correct_params(
        self, mock_router: AsyncMock
    ) -> None:
        """_generate passes correct params to router."""
        agent = OutlineAgent(
            mock_router, strategy="quality", temperature=0.5, max_tokens=8192
        )
        prepared = PreparedOutlinePrompt(
            system_prompt="System prompt.",
            user_prompt="User prompt.",
            prompt_version="v1_outline",
        )
        await agent._generate(prepared)

        mock_router.complete_structured.assert_called_once()
        call_kwargs = mock_router.complete_structured.call_args.kwargs
        assert call_kwargs["action"] == "material_outline"
        assert call_kwargs["response_schema"] is MaterialOutline
        assert call_kwargs["system_prompt"] == "System prompt."
        assert call_kwargs["prompt"] == "User prompt."
        assert call_kwargs["strategy"] == "quality"
        assert call_kwargs["temperature"] == 0.5
        assert call_kwargs["max_tokens"] == 8192

    @pytest.mark.asyncio
    async def test_propagates_all_models_failed(self, mock_router: AsyncMock) -> None:
        """_generate propagates AllModelsFailedError from router."""
        mock_router.complete_structured.side_effect = AllModelsFailedError(
            action="material_outline",
            strategies_tried=["default"],
            errors=[("gemini-2.5-flash", "rate limit")],
        )
        agent = OutlineAgent(mock_router)
        prepared = PreparedOutlinePrompt(
            system_prompt="System.",
            user_prompt="User.",
            prompt_version="v1_outline",
        )
        with pytest.raises(AllModelsFailedError):
            await agent._generate(prepared)


class TestOutlineAgentRun:
    """Integration tests for full run() pipeline."""

    @pytest.mark.asyncio
    async def test_run_end_to_end(
        self,
        mock_router: AsyncMock,
        sample_source_doc: SourceDocument,
        prompt_data: PromptData,
    ) -> None:
        """run() orchestrates prepare + generate and returns outline."""
        with patch(
            "course_supporter.agents.outline.load_prompt",
            return_value=prompt_data,
        ):
            agent = OutlineAgent(mock_router)
            result = await agent.run(sample_source_doc)

        assert isinstance(result, MaterialOutline)
        assert result.title == "Python Basics"
        assert len(result.sections) == 1

    @pytest.mark.asyncio
    async def test_run_passes_source_doc_to_router(
        self,
        mock_router: AsyncMock,
        sample_source_doc: SourceDocument,
        prompt_data: PromptData,
    ) -> None:
        """run() serializes source doc and passes it through to router."""
        with patch(
            "course_supporter.agents.outline.load_prompt",
            return_value=prompt_data,
        ):
            agent = OutlineAgent(mock_router)
            await agent.run(sample_source_doc)

        call_kwargs = mock_router.complete_structured.call_args.kwargs
        assert "file:///lecture.mp4" in call_kwargs["prompt"]
        assert call_kwargs["system_prompt"] == "You are an outline generator."


class TestRunWithMetadata:
    """Tests for run_with_metadata() — returns OutlineResult."""

    @pytest.mark.asyncio
    async def test_returns_outline_result(
        self,
        mock_router: AsyncMock,
        sample_source_doc: SourceDocument,
        prompt_data: PromptData,
    ) -> None:
        """run_with_metadata returns OutlineResult with all fields."""
        with patch(
            "course_supporter.agents.outline.load_prompt",
            return_value=prompt_data,
        ):
            agent = OutlineAgent(mock_router)
            result = await agent.run_with_metadata(sample_source_doc)

        assert isinstance(result, OutlineResult)
        assert isinstance(result.outline, MaterialOutline)
        assert result.prompt_version == "v1_outline"
        assert result.response.model_id == "gemini-2.5-flash"
        assert result.response.tokens_in == 500
        assert result.response.tokens_out == 1000
