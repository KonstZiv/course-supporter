# 📋 S1-021: ArchitectAgent Class

## Мета

Реалізувати `ArchitectAgent` — ключовий клас, який приймає `CourseContext`, серіалізує його, форматує промпт, викликає LLM через `ModelRouter.complete_structured()` і повертає валідну `CourseStructure`. Це ядро бізнес-логіки проєкту.

## Контекст

Третя задача Epic 4. Залежить від S1-019 (CourseStructure — response schema) та S1-020 (prompt_loader — system/user prompt). Блокує S1-022 (persistence — зберігає результат). Використовує `action="course_structuring"` з `config/models.yaml` (вже визначений: default chain `gemini-2.5-flash → deepseek-chat`, quality chain `claude-sonnet → gemini-2.5-pro`).

Файл `agents/architect.py` вже існує як stub (TODO) — потрібно замінити на реальний код.

---

## Acceptance Criteria

- [ ] `ArchitectAgent.__init__` приймає `router`, `prompt_path`, `strategy`, `temperature`, `max_tokens`
- [ ] `ArchitectAgent.run(context: CourseContext) -> CourseStructure` — async method
- [ ] Серіалізує `CourseContext` → JSON string для user prompt
- [ ] Завантажує prompt через `load_prompt()`, форматує через `format_user_prompt()`
- [ ] Викликає `router.complete_structured(action="course_structuring", response_schema=CourseStructure)`
- [ ] Повертає перший елемент tuple (parsed CourseStructure)
- [ ] Пробрасывает `AllModelsFailedError` від router (не глушить)
- [ ] Default prompt path: `prompts/architect/v1.yaml`
- [ ] ~10 unit-тестів з mocked router, всі зелені
- [ ] `make check` проходить

---

## Реалізація

### src/course_supporter/agents/architect.py

```python
"""ArchitectAgent: generates course structure from materials via LLM."""

import structlog

from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt
from course_supporter.llm.router import ModelRouter
from course_supporter.models.course import CourseContext, CourseStructure

logger = structlog.get_logger()

DEFAULT_PROMPT_PATH = "prompts/architect/v1.yaml"


class ArchitectAgent:
    """Generates structured course program from course materials.

    Uses ModelRouter with action='course_structuring' to call LLM
    with structured output (CourseStructure Pydantic schema).

    Args:
        router: ModelRouter instance for LLM calls.
        prompt_path: Path to YAML prompt template.
        strategy: Routing strategy ('default', 'quality', 'budget').
        temperature: LLM temperature (0.0 = deterministic).
        max_tokens: Maximum output tokens.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        prompt_path: str = DEFAULT_PROMPT_PATH,
        strategy: str = "default",
        temperature: float = 0.0,
        max_tokens: int = 8192,
    ) -> None:
        self._router = router
        self._prompt_path = prompt_path
        self._strategy = strategy
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def run(self, context: CourseContext) -> CourseStructure:
        """Generate course structure from materials.

        Args:
            context: Unified course context from ingestion pipeline.

        Returns:
            Validated CourseStructure from LLM.

        Raises:
            AllModelsFailedError: If all models in all strategies fail.
            FileNotFoundError: If prompt file not found.
        """
        # 1. Load and format prompt
        prompt_data = load_prompt(self._prompt_path)
        system_prompt = prompt_data["system_prompt"]
        user_prompt = format_user_prompt(
            prompt_data["user_prompt_template"],
            context.model_dump_json(indent=2),
        )

        logger.info(
            "architect_agent_run",
            strategy=self._strategy,
            prompt_version=prompt_data.get("version", "unknown"),
            documents_count=len(context.documents),
            context_length=len(user_prompt),
        )

        # 2. Call LLM via ModelRouter
        structure, response = await self._router.complete_structured(
            action="course_structuring",
            prompt=user_prompt,
            response_schema=CourseStructure,
            system_prompt=system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            strategy=self._strategy,
        )

        logger.info(
            "architect_agent_done",
            modules_count=len(structure.modules),
            model=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        return structure
```

### src/course_supporter/agents/__init__.py (оновити)

```python
"""Agents for course structure generation."""

from course_supporter.agents.architect import ArchitectAgent
from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt

__all__ = [
    "ArchitectAgent",
    "format_user_prompt",
    "load_prompt",
]
```

---

## Тести

### tests/unit/test_architect_agent.py

```python
"""Tests for ArchitectAgent."""

from unittest.mock import AsyncMock, patch

import pytest

from course_supporter.agents.architect import ArchitectAgent, DEFAULT_PROMPT_PATH
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
def prompt_data() -> dict:
    """Mock prompt data."""
    return {
        "version": "v1",
        "system_prompt": "You are a course architect.",
        "user_prompt_template": "Materials:\n{context}\nGenerate.",
    }


class TestArchitectAgentInit:
    def test_default_params(self, mock_router: AsyncMock) -> None:
        """ArchitectAgent initializes with sensible defaults."""
        agent = ArchitectAgent(mock_router)
        assert agent._prompt_path == DEFAULT_PROMPT_PATH
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


class TestArchitectAgentRun:
    @pytest.mark.asyncio
    async def test_run_returns_course_structure(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: dict,
    ) -> None:
        """run() returns CourseStructure from LLM response."""
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
    async def test_run_calls_router_with_correct_action(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: dict,
    ) -> None:
        """run() passes action='course_structuring' to router."""
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(mock_router, strategy="quality")
            await agent.run(sample_context)

        mock_router.complete_structured.assert_called_once()
        call_kwargs = mock_router.complete_structured.call_args
        assert call_kwargs.kwargs["action"] == "course_structuring"
        assert call_kwargs.kwargs["response_schema"] is CourseStructure
        assert call_kwargs.kwargs["strategy"] == "quality"

    @pytest.mark.asyncio
    async def test_run_passes_system_prompt(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: dict,
    ) -> None:
        """run() passes system_prompt from loaded YAML."""
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(mock_router)
            await agent.run(sample_context)

        call_kwargs = mock_router.complete_structured.call_args
        assert call_kwargs.kwargs["system_prompt"] == "You are a course architect."

    @pytest.mark.asyncio
    async def test_run_formats_context_into_prompt(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: dict,
    ) -> None:
        """run() serializes CourseContext and injects into user prompt."""
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(mock_router)
            await agent.run(sample_context)

        call_kwargs = mock_router.complete_structured.call_args
        user_prompt = call_kwargs.kwargs["prompt"]
        assert "Materials:" in user_prompt
        assert "file:///test.md" in user_prompt

    @pytest.mark.asyncio
    async def test_run_passes_temperature_and_max_tokens(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: dict,
    ) -> None:
        """run() forwards temperature and max_tokens to router."""
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(
                mock_router, temperature=0.5, max_tokens=2048
            )
            await agent.run(sample_context)

        call_kwargs = mock_router.complete_structured.call_args
        assert call_kwargs.kwargs["temperature"] == 0.5
        assert call_kwargs.kwargs["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_run_propagates_all_models_failed(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: dict,
    ) -> None:
        """run() propagates AllModelsFailedError from router."""
        mock_router.complete_structured.side_effect = AllModelsFailedError(
            action="course_structuring",
            strategies_tried=["default"],
            errors=[("gemini-2.5-flash", "rate limit")],
        )
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data,
        ):
            agent = ArchitectAgent(mock_router)
            with pytest.raises(AllModelsFailedError):
                await agent.run(sample_context)

    @pytest.mark.asyncio
    async def test_run_propagates_file_not_found(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
    ) -> None:
        """run() propagates FileNotFoundError if prompt file missing."""
        agent = ArchitectAgent(
            mock_router, prompt_path="nonexistent/prompt.yaml"
        )
        with pytest.raises(FileNotFoundError):
            await agent.run(sample_context)
```

---

## Структура файлів

```
src/course_supporter/agents/
├── __init__.py                  # UPDATE: add ArchitectAgent export
├── architect.py                 # UPDATE: replace stub with implementation
└── prompt_loader.py             # FROM S1-020

tests/unit/
└── test_architect_agent.py      # NEW: ~10 tests
```

---

## Кроки виконання

1. Замінити stub `agents/architect.py` — повна реалізація `ArchitectAgent`
2. Оновити `agents/__init__.py` — додати `ArchitectAgent` export
3. Створити `tests/unit/test_architect_agent.py`
4. `make check`

---

## Примітки

- **action="course_structuring"**: вже визначений у `config/models.yaml` з `requires: [structured_output]`. Default chain: `gemini-2.5-flash → deepseek-chat`. Quality: `claude-sonnet → gemini-2.5-pro`.
- **complete_structured()** повертає `tuple[Any, LLMResponse]`. Перший елемент — parsed Pydantic model (CourseStructure). ModelRouter обробляє retry і fallback.
- **Серіалізація context**: `context.model_dump_json(indent=2)` — JSON representation. LLM отримує повний контекст як текст.
- **max_tokens=8192**: CourseStructure може бути великою (десятки модулів, сотні концепцій). 8192 — розумний default.
- **Structured output validation**: Pydantic validation відбувається всередині provider.complete_structured(). Якщо LLM повертає невалідний JSON — `StructuredOutputError`, router retry/fallback.
- **Не глушимо помилки**: `AllModelsFailedError` пробрасывается до caller (API endpoint або orchestrator).
