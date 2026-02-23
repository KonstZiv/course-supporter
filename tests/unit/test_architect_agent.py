"""Tests for ArchitectAgent."""

from unittest.mock import AsyncMock, patch

import pytest

from course_supporter.agents.architect import (
    PROMPT_PATHS,
    ArchitectAgent,
    GenerationResult,
    PreparedPrompt,
)
from course_supporter.agents.prompt_loader import PromptData
from course_supporter.llm.router import AllModelsFailedError
from course_supporter.llm.schemas import LLMResponse
from course_supporter.models.course import (
    CourseContext,
    CourseStructure,
    ModuleOutput,
)
from course_supporter.models.source import SourceDocument


@pytest.fixture()
def mock_router() -> AsyncMock:
    """ModelRouter mock with complete_structured returning CourseStructure."""
    router = AsyncMock()
    structure = CourseStructure(
        title="Test Course",
        description="A test course",
        modules=[ModuleOutput(title="Module 1")],
    )
    response = LLMResponse(
        content="{}",
        provider="gemini",
        model_id="gemini-2.5-flash",
        tokens_in=100,
        tokens_out=200,
    )
    router.complete_structured.return_value = (structure, response)
    return router


@pytest.fixture()
def sample_context() -> CourseContext:
    """Minimal CourseContext for testing."""
    doc = SourceDocument(source_type="text", source_url="file:///test.md")
    return CourseContext(documents=[doc])


@pytest.fixture()
def prompt_data() -> PromptData:
    """Mock prompt data."""
    return PromptData(
        version="v1",
        system_prompt="You are a course architect.",
        user_prompt_template="Materials:\n{context}\nGenerate.",
    )


class TestArchitectAgentInit:
    def test_default_params(self, mock_router: AsyncMock) -> None:
        """ArchitectAgent initializes with sensible defaults."""
        agent = ArchitectAgent(mock_router)
        assert agent._prompt_path == PROMPT_PATHS["free"]
        assert agent._mode == "free"
        assert agent._strategy == "default"
        assert agent._temperature == 0.0
        assert agent._max_tokens == 8192

    def test_custom_params(self, mock_router: AsyncMock) -> None:
        """ArchitectAgent accepts custom parameters."""
        agent = ArchitectAgent(
            mock_router,
            prompt_path="custom/prompt.yaml",
            strategy="quality",
            temperature=0.3,
            max_tokens=4096,
        )
        assert agent._prompt_path == "custom/prompt.yaml"
        assert agent._strategy == "quality"
        assert agent._temperature == 0.3
        assert agent._max_tokens == 4096

    def test_free_mode_uses_free_prompt(self, mock_router: AsyncMock) -> None:
        """Free mode selects v1.yaml prompt."""
        agent = ArchitectAgent(mock_router, mode="free")
        assert agent._prompt_path == PROMPT_PATHS["free"]
        assert agent._mode == "free"

    def test_guided_mode_uses_guided_prompt(self, mock_router: AsyncMock) -> None:
        """Guided mode selects v1_guided.yaml prompt."""
        agent = ArchitectAgent(mock_router, mode="guided")
        assert agent._prompt_path == PROMPT_PATHS["guided"]
        assert agent._mode == "guided"

    def test_explicit_prompt_path_overrides_mode(self, mock_router: AsyncMock) -> None:
        """Explicit prompt_path overrides mode-based selection."""
        agent = ArchitectAgent(
            mock_router, mode="guided", prompt_path="custom/override.yaml"
        )
        assert agent._prompt_path == "custom/override.yaml"


class TestPreparePrompts:
    """Tests for _prepare_prompts step (independent of LLM)."""

    def test_returns_prepared_prompt(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: PromptData,
    ) -> None:
        """_prepare_prompts returns PreparedPrompt with correct fields."""
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(mock_router)
            prepared = agent._prepare_prompts(sample_context)

        assert isinstance(prepared, PreparedPrompt)
        assert prepared.system_prompt == "You are a course architect."
        assert prepared.prompt_version == "v1"

    def test_serializes_context_into_user_prompt(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: PromptData,
    ) -> None:
        """_prepare_prompts injects serialized context into user prompt."""
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(mock_router)
            prepared = agent._prepare_prompts(sample_context)

        assert "Materials:" in prepared.user_prompt
        assert "file:///test.md" in prepared.user_prompt
        assert "{context}" not in prepared.user_prompt

    def test_propagates_file_not_found(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
    ) -> None:
        """_prepare_prompts propagates FileNotFoundError."""
        agent = ArchitectAgent(mock_router, prompt_path="nonexistent/prompt.yaml")
        with pytest.raises(FileNotFoundError):
            agent._prepare_prompts(sample_context)

    def test_default_version_when_missing(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
    ) -> None:
        """_prepare_prompts uses 'unknown' when version key is absent."""
        prompt_no_version = PromptData(
            system_prompt="System prompt.",
            user_prompt_template="{context}",
        )
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_no_version,
        ):
            agent = ArchitectAgent(mock_router)
            prepared = agent._prepare_prompts(sample_context)

        assert prepared.prompt_version == "unknown"


class TestGuidedModePrepare:
    """Tests for guided mode prompt preparation with existing_structure."""

    def test_existing_structure_in_prompt(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
    ) -> None:
        """_prepare_prompts injects existing_structure into guided template."""
        guided_data = PromptData(
            version="v1_guided",
            system_prompt="Guided system.",
            user_prompt_template=(
                "Structure:\n{existing_structure}\nMaterials:\n{context}"
            ),
        )
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=guided_data,
        ):
            agent = ArchitectAgent(mock_router, mode="guided")
            prepared = agent._prepare_prompts(
                sample_context,
                existing_structure='[{"title": "Module 1"}]',
            )

        assert "Module 1" in prepared.user_prompt
        assert prepared.prompt_version == "v1_guided"
        assert "{existing_structure}" not in prepared.user_prompt

    def test_free_mode_no_existing_structure(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: PromptData,
    ) -> None:
        """Free mode does not pass existing_structure."""
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(mock_router, mode="free")
            prepared = agent._prepare_prompts(sample_context)

        # No KeyError — free template has no {existing_structure} placeholder
        assert "file:///test.md" in prepared.user_prompt


class TestGenerate:
    """Tests for _generate step (LLM interaction)."""

    @pytest.mark.asyncio
    async def test_returns_course_structure(self, mock_router: AsyncMock) -> None:
        """_generate returns (CourseStructure, LLMResponse) tuple."""
        agent = ArchitectAgent(mock_router)
        prepared = PreparedPrompt(
            system_prompt="System.",
            user_prompt="User prompt.",
            prompt_version="v1",
        )
        structure, response = await agent._generate(prepared)

        assert isinstance(structure, CourseStructure)
        assert structure.title == "Test Course"
        assert len(structure.modules) == 1
        assert response.model_id == "gemini-2.5-flash"

    @pytest.mark.asyncio
    async def test_calls_router_with_correct_params(
        self, mock_router: AsyncMock
    ) -> None:
        """_generate passes correct params to router."""
        agent = ArchitectAgent(
            mock_router, strategy="quality", temperature=0.5, max_tokens=2048
        )
        prepared = PreparedPrompt(
            system_prompt="System prompt.",
            user_prompt="User prompt.",
            prompt_version="v1",
        )
        await agent._generate(prepared)

        mock_router.complete_structured.assert_called_once()
        call_kwargs = mock_router.complete_structured.call_args.kwargs
        assert call_kwargs["action"] == "course_structuring"
        assert call_kwargs["response_schema"] is CourseStructure
        assert call_kwargs["system_prompt"] == "System prompt."
        assert call_kwargs["prompt"] == "User prompt."
        assert call_kwargs["strategy"] == "quality"
        assert call_kwargs["temperature"] == 0.5
        assert call_kwargs["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_propagates_all_models_failed(self, mock_router: AsyncMock) -> None:
        """_generate propagates AllModelsFailedError from router."""
        mock_router.complete_structured.side_effect = AllModelsFailedError(
            action="course_structuring",
            strategies_tried=["default"],
            errors=[("gemini-2.5-flash", "rate limit")],
        )
        agent = ArchitectAgent(mock_router)
        prepared = PreparedPrompt(
            system_prompt="System.",
            user_prompt="User.",
            prompt_version="v1",
        )
        with pytest.raises(AllModelsFailedError):
            await agent._generate(prepared)


class TestArchitectAgentRun:
    """Integration tests for full run() pipeline."""

    @pytest.mark.asyncio
    async def test_run_end_to_end(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: PromptData,
    ) -> None:
        """run() orchestrates prepare + generate and returns structure."""
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(mock_router)
            result = await agent.run(sample_context)

        assert isinstance(result, CourseStructure)
        assert result.title == "Test Course"
        assert len(result.modules) == 1

    @pytest.mark.asyncio
    async def test_run_passes_context_to_router(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: PromptData,
    ) -> None:
        """run() serializes context and passes it through to router."""
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(mock_router)
            await agent.run(sample_context)

        call_kwargs = mock_router.complete_structured.call_args.kwargs
        assert "file:///test.md" in call_kwargs["prompt"]
        assert call_kwargs["system_prompt"] == "You are a course architect."


class TestRunWithMetadata:
    """Tests for run_with_metadata() — returns GenerationResult."""

    @pytest.mark.asyncio
    async def test_returns_generation_result(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: PromptData,
    ) -> None:
        """run_with_metadata returns GenerationResult with all fields."""
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(mock_router)
            result = await agent.run_with_metadata(sample_context)

        assert isinstance(result, GenerationResult)
        assert result.structure.title == "Test Course"
        assert result.prompt_version == "v1"
        assert result.response.model_id == "gemini-2.5-flash"
        assert result.response.tokens_in == 100
        assert result.response.tokens_out == 200

    @pytest.mark.asyncio
    async def test_metadata_includes_cost(
        self,
        sample_context: CourseContext,
        prompt_data: PromptData,
    ) -> None:
        """run_with_metadata propagates cost_usd from LLM response."""
        router = AsyncMock()
        structure = CourseStructure(
            title="Cost Test",
            modules=[ModuleOutput(title="M1")],
        )
        response = LLMResponse(
            content="{}",
            provider="anthropic",
            model_id="claude-sonnet-4",
            tokens_in=500,
            tokens_out=1000,
            cost_usd=0.042,
        )
        router.complete_structured.return_value = (structure, response)

        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(router)
            result = await agent.run_with_metadata(sample_context)

        assert result.response.cost_usd == 0.042
        assert result.response.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_guided_mode_passes_existing_structure(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
    ) -> None:
        """run_with_metadata passes existing_structure to prompt preparation."""
        guided_data = PromptData(
            version="v1_guided",
            system_prompt="Guided system.",
            user_prompt_template=(
                "Structure:\n{existing_structure}\nMaterials:\n{context}"
            ),
        )
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=guided_data,
        ):
            agent = ArchitectAgent(mock_router, mode="guided")
            result = await agent.run_with_metadata(
                sample_context,
                existing_structure='[{"title": "Existing Module"}]',
            )

        assert result.prompt_version == "v1_guided"
        # Verify the prompt contained the existing structure
        call_kwargs = mock_router.complete_structured.call_args.kwargs
        assert "Existing Module" in call_kwargs["prompt"]
