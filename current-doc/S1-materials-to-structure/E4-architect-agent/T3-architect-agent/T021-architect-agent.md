# 📋 S1-021: ArchitectAgent Class

## Мета

Реалізувати `ArchitectAgent` — ключовий клас, який приймає `CourseContext`, серіалізує його, форматує промпт, викликає LLM через `ModelRouter.complete_structured()` і повертає валідну `CourseStructure`. Це ядро бізнес-логіки проєкту.

## Контекст

Третя задача Epic 4. Залежить від S1-019 (CourseStructure — response schema) та S1-020 (prompt_loader — system/user prompt). Блокує S1-022 (persistence — зберігає результат). Використовує `action="course_structuring"` з `config/models.yaml` (вже визначений: default chain `gemini-2.5-flash → deepseek-chat`, quality chain `claude-sonnet → gemini-2.5-pro`).

Файл `agents/architect.py` вже існує як stub (TODO) — потрібно замінити на реальний код.

---

## Архітектурне рішення: Step-Based Design

### Мотивація

Монолітний `run()` з єдиним LLM-викликом достатній для MVP, але створює проблеми при міграції на ланцюги/графи (LangGraph, custom DAG):

- Не можна вставити hook між кроками (логування, людський фідбек, валідація)
- Немає проміжного стану для streaming або часткових результатів
- Неможливо розбити на ноди графа без рефакторингу

### Рішення

Розбити `run()` на **окремі методи-кроки** з проміжним типом `PreparedPrompt`:

```
run(context)
  ├─ _prepare_prompts(context) → PreparedPrompt    # step 1: load & format
  └─ _generate(prepared)       → CourseStructure   # step 2: call LLM
```

### Переваги

1. **Кожен метод — потенційна нода** в LangGraph або step у ланцюгу
2. **Проміжні типи** (`PreparedPrompt`) стають частиною State графа
3. **Легко додати кроки**: `_validate()`, `_refine()`, `_chunk()` без рефакторингу
4. **Per-step тестування**: можна тестувати prompt formatting окремо від LLM
5. **Мінімальний overhead**: для MVP — лінійний виклик, але готовий до розширення

### Міграційний шлях (Epic 7+)

```
# Сьогодні (MVP): лінійний виклик
run() → _prepare_prompts() → _generate() → return

# Завтра (LangGraph): кожен метод стає нодою
START → prepare_prompts_node → generate_node → validate_node → END
                                    ↑                  |
                                    └── retry_edge ←───┘

# Завтра (multi-step): розбити _generate на sub-steps
_generate_modules() → _generate_lessons(per module) → _generate_exercises()
```

---

## Acceptance Criteria

- [ ] `ArchitectAgent.__init__` приймає `router`, `prompt_path`, `strategy`, `temperature`, `max_tokens`
- [ ] `ArchitectAgent.run(context: CourseContext) -> CourseStructure` — async orchestrator
- [ ] `_prepare_prompts(context) -> PreparedPrompt` — sync, load YAML + serialize context
- [ ] `_generate(prepared: PreparedPrompt) -> CourseStructure` — async, call router
- [ ] `PreparedPrompt` — NamedTuple з system_prompt, user_prompt, prompt_version
- [ ] Викликає `router.complete_structured(action="course_structuring", response_schema=CourseStructure)`
- [ ] Повертає перший елемент tuple (parsed CourseStructure)
- [ ] Пробрасывает `AllModelsFailedError` від router (не глушить)
- [ ] Default prompt path: `prompts/architect/v1.yaml`
- [ ] ~12 unit-тестів з mocked router, всі зелені (включаючи per-step тести)
- [ ] `make check` проходить

---

## Реалізація

### src/course_supporter/agents/architect.py

```python
"""ArchitectAgent: generates course structure from materials via LLM."""

from typing import NamedTuple

import structlog

from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt
from course_supporter.llm.router import ModelRouter
from course_supporter.models.course import CourseContext, CourseStructure

logger = structlog.get_logger()

DEFAULT_PROMPT_PATH = "prompts/architect/v1.yaml"


class PreparedPrompt(NamedTuple):
    """Intermediate result of prompt preparation step.

    Separates prompt loading/formatting from LLM invocation,
    enabling independent testing and future graph-based orchestration
    where each step becomes a node.
    """

    system_prompt: str
    user_prompt: str
    prompt_version: str


class ArchitectAgent:
    """Generates structured course program from course materials.

    Uses ModelRouter with action='course_structuring' to call LLM
    with structured output (CourseStructure Pydantic schema).

    Architecture: step-based design for future chain/graph migration.
    Each step is a separate method that can become a node in a
    LangGraph or custom DAG pipeline.

    Steps:
        1. _prepare_prompts: load YAML template, serialize context, format prompt
        2. _generate: call LLM via ModelRouter, return validated structure

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

        Orchestrates the pipeline: prepare prompts → generate via LLM.

        Args:
            context: Unified course context from ingestion pipeline.

        Returns:
            Validated CourseStructure from LLM.

        Raises:
            AllModelsFailedError: If all models in all strategies fail.
            FileNotFoundError: If prompt file not found.
        """
        prepared = self._prepare_prompts(context)
        return await self._generate(prepared, documents_count=len(context.documents))

    def _prepare_prompts(self, context: CourseContext) -> PreparedPrompt:
        """Step 1: Load prompt template and format with context.

        Loads YAML prompt file, serializes CourseContext to JSON,
        and injects it into the user prompt template.

        Args:
            context: Course context to serialize into the prompt.

        Returns:
            PreparedPrompt with system prompt, formatted user prompt,
            and prompt version for logging/A/B testing.

        Raises:
            FileNotFoundError: If prompt YAML file not found.
            KeyError: If YAML missing required keys.
        """
        prompt_data = load_prompt(self._prompt_path)
        system_prompt = prompt_data["system_prompt"]
        user_prompt = format_user_prompt(
            prompt_data["user_prompt_template"],
            context.model_dump_json(indent=2),
        )
        return PreparedPrompt(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=prompt_data.get("version", "unknown"),
        )

    async def _generate(
        self,
        prepared: PreparedPrompt,
        *,
        documents_count: int = 0,
    ) -> CourseStructure:
        """Step 2: Call LLM and return validated CourseStructure.

        Sends prepared prompts to ModelRouter with structured output
        and returns the parsed Pydantic model.

        Args:
            prepared: Formatted prompts from _prepare_prompts step.
            documents_count: Number of documents for logging.

        Returns:
            Validated CourseStructure from LLM.

        Raises:
            AllModelsFailedError: If all models in all strategies fail.
        """
        logger.info(
            "architect_agent_generating",
            strategy=self._strategy,
            prompt_version=prepared.prompt_version,
            documents_count=documents_count,
            context_length=len(prepared.user_prompt),
        )

        structure, response = await self._router.complete_structured(
            action="course_structuring",
            prompt=prepared.user_prompt,
            response_schema=CourseStructure,
            system_prompt=prepared.system_prompt,
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

from course_supporter.agents.architect import ArchitectAgent, PreparedPrompt
from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt

__all__ = [
    "ArchitectAgent",
    "PreparedPrompt",
    "format_user_prompt",
    "load_prompt",
]
```

---

## Тести

### tests/unit/test_architect_agent.py

```python
"""Tests for ArchitectAgent."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from course_supporter.agents.architect import (
    DEFAULT_PROMPT_PATH,
    ArchitectAgent,
    PreparedPrompt,
)
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
def prompt_data() -> dict[str, Any]:
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


class TestPreparePrompts:
    """Tests for _prepare_prompts step (independent of LLM)."""

    def test_returns_prepared_prompt(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
        prompt_data: dict[str, Any],
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
        prompt_data: dict[str, Any],
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
        agent = ArchitectAgent(
            mock_router, prompt_path="nonexistent/prompt.yaml"
        )
        with pytest.raises(FileNotFoundError):
            agent._prepare_prompts(sample_context)

    def test_default_version_when_missing(
        self,
        mock_router: AsyncMock,
        sample_context: CourseContext,
    ) -> None:
        """_prepare_prompts uses 'unknown' when version key is absent."""
        prompt_data_no_version = {
            "system_prompt": "System prompt.",
            "user_prompt_template": "{context}",
        }
        with patch(
            "course_supporter.agents.architect.load_prompt",
            return_value=prompt_data_no_version,
        ):
            agent = ArchitectAgent(mock_router)
            prepared = agent._prepare_prompts(sample_context)

        assert prepared.prompt_version == "unknown"


class TestGenerate:
    """Tests for _generate step (LLM interaction)."""

    @pytest.mark.asyncio
    async def test_returns_course_structure(
        self, mock_router: AsyncMock
    ) -> None:
        """_generate returns CourseStructure from router response."""
        agent = ArchitectAgent(mock_router)
        prepared = PreparedPrompt(
            system_prompt="System.",
            user_prompt="User prompt.",
            prompt_version="v1",
        )
        result = await agent._generate(prepared)

        assert isinstance(result, CourseStructure)
        assert result.title == "Test Course"
        assert len(result.modules) == 1

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
    async def test_propagates_all_models_failed(
        self, mock_router: AsyncMock
    ) -> None:
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
        prompt_data: dict[str, Any],
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
        prompt_data: dict[str, Any],
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
```

---

## Структура файлів

```
src/course_supporter/agents/
├── __init__.py                  # UPDATE: add ArchitectAgent, PreparedPrompt exports
├── architect.py                 # UPDATE: replace stub with step-based implementation
└── prompt_loader.py             # FROM S1-020

tests/unit/
└── test_architect_agent.py      # NEW: ~12 tests (per-step + integration)
```

---

## Кроки виконання

1. Замінити stub `agents/architect.py` — `PreparedPrompt` + step-based `ArchitectAgent`
2. Оновити `agents/__init__.py` — додати `ArchitectAgent`, `PreparedPrompt` exports
3. Створити `tests/unit/test_architect_agent.py`
4. `make check`

---

## Примітки

- **action="course_structuring"**: вже визначений у `config/models.yaml` з `requires: [structured_output]`. Default chain: `gemini-2.5-flash → deepseek-chat`. Quality: `claude-sonnet → gemini-2.5-pro`.
- **complete_structured()** повертає `tuple[Any, LLMResponse]`. Перший елемент — parsed Pydantic model (CourseStructure). ModelRouter обробляє retry і fallback.
- **PreparedPrompt**: `NamedTuple` (не dataclass) — immutable, lightweight, unpacking-friendly. Стане частиною `GraphState` при міграції на LangGraph.
- **Step isolation**: `_prepare_prompts` — sync (CPU-only), `_generate` — async (I/O). Різна природа дозволяє різне масштабування.
- **Серіалізація context**: `context.model_dump_json(indent=2)` — JSON representation. LLM отримує повний контекст як текст.
- **max_tokens=8192**: CourseStructure може бути великою (десятки модулів, сотні концепцій). 8192 — розумний default.
- **Structured output validation**: Pydantic validation відбувається всередині provider.complete_structured(). Якщо LLM повертає невалідний JSON — `StructuredOutputError`, router retry/fallback.
- **Не глушимо помилки**: `AllModelsFailedError` пробрасывается до caller (API endpoint або orchestrator).

## Шлях міграції на графи/ланцюги

| Сценарій | Що потрібно | Effort |
|----------|-------------|--------|
| **Додати validation step** | Новий метод `_validate(structure) -> CourseStructure`, виклик між `_generate` і `return` | ~1 год |
| **Додати refinement loop** | `_refine(structure, feedback) -> CourseStructure`, loop у `run()` або edge у графі | ~2 год |
| **Multi-step generation** | Розбити `_generate` на `_generate_modules()` + `_generate_lessons()` + `_generate_exercises()` | ~4 год |
| **LangGraph міграція** | Кожен `_method` стає `@node`, `PreparedPrompt` входить у `GraphState`, edges = conditional routing | ~8 год |
| **Custom DAG** | Обгортка `Pipeline(nodes=[prepare, generate, validate])` з logging per-node | ~4 год |
