# 📋 S1-009: ModelRouter

## Мета

Центральна точка для всіх LLM-викликів. Приймає action + prompt + optional strategy, обирає chain моделей, обробляє retry/fallback двох рівнів: всередині chain (модель → модель) та між strategies (requested chain впав → default chain). Перевіряє `provider.enabled` перед викликом. Класифікує помилки: permanent (401, 403) — skip одразу, transient (429, 500) — retry.

## Контекст

Залежить від S1-007 (providers з enable/disable) та S1-008 (registry з strategies). Використовується усіма наступними компонентами.

---

## Acceptance Criteria

- [x] `router.complete(action, prompt)` — default strategy, fallback всередині chain
- [x] `router.complete(action, prompt, strategy="quality")` — explicit strategy
- [x] Disabled provider → skip, try next в chain
- [x] Весь requested chain впав → fallback на `default` strategy (якщо не вже default)
- [x] Всі strategies вичерпані → `AllModelsFailedError` з деталями
- [x] Retry до max_attempts на кожну модель (transient errors only)
- [x] Permanent errors (401, 403, 400, 404) → skip модель одразу, без retry
- [x] Cost enrichment — автоматичний `cost_usd` через `ModelConfig.estimate_cost()`
- [x] LogCallback для запису в DB (S1-010)
- [x] action/strategy проставляються в LLMResponse
- [x] `request.model` = `model_cfg.model_id` — провайдер використовує конкретну модель з chain
- [x] DRY: complete/complete_structured через спільний `_execute_with_fallback`

---

## Зміни в існуючих файлах

### src/course_supporter/llm/schemas.py

Додано поле `model` в `LLMRequest`:

```python
class LLMRequest(BaseModel):
    prompt: str
    system_prompt: str | None = None
    model: str = ""  # set by ModelRouter; providers fall back to default_model
    temperature: float = 0.0
    max_tokens: int = 4096
    action: str = ""
    strategy: str = "default"
```

### Провайдери (gemini.py, anthropic.py, openai_compat.py)

Кожен провайдер тепер використовує `request.model or self._default_model`:

```python
async def complete(self, request: LLMRequest) -> LLMResponse:
    model = request.model or self._default_model
    # ... SDK call з model, LLMResponse з model_id=model
```

Це дозволяє router-у передавати конкретну модель з chain (напр. `gemini-2.5-pro` замість дефолтного `gemini-2.5-flash`).

---

## src/course_supporter/llm/router.py

```python
"""ModelRouter -- central entry point for all LLM calls.

Two-level fallback:
1. Within chain: model 1 -> model 2 -> model 3
2. Between strategies: quality chain failed -> fallback to default chain
"""

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider, StructuredOutputError
from course_supporter.llm.registry import ModelConfig, ModelRegistryConfig
from course_supporter.llm.schemas import LLMRequest, LLMResponse

logger = structlog.get_logger()

LogCallback = Callable[[LLMResponse, bool, str | None], Awaitable[None]]


class AllModelsFailedError(Exception):
    """All models in all attempted strategies failed."""

    def __init__(
        self,
        action: str,
        strategies_tried: list[str],
        errors: list[tuple[str, str]],
    ) -> None:
        self.action = action
        self.strategies_tried = strategies_tried
        self.errors = errors
        details = "; ".join(f"{m}: {e}" for m, e in errors)
        super().__init__(
            f"All models failed for action '{action}' "
            f"(strategies: {strategies_tried}): {details}"
        )


class ModelRouter:
    """Routes LLM requests with strategy-based fallback."""

    def __init__(
        self,
        providers: dict[str, LLMProvider],
        registry: ModelRegistryConfig,
        log_callback: LogCallback | None = None,
        max_attempts: int = 2,
    ) -> None:
        self._providers = providers
        self._registry = registry
        self._log_callback = log_callback
        self._max_attempts = max_attempts

    # -- public API -------------------------------------------------

    async def complete(
        self, action: str, prompt: str, *,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        strategy: str = "default",
    ) -> LLMResponse:
        """Generate text completion with strategy-based fallback."""
        request = LLMRequest(...)

        async def call_fn(provider, req) -> LLMResponse:
            return await provider.complete(req)

        return await self._execute_with_fallback(
            action, strategy, request, call_fn,
        )

    async def complete_structured(
        self, action: str, prompt: str,
        response_schema: type[BaseModel], *,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        strategy: str = "default",
    ) -> tuple[Any, LLMResponse]:
        """Generate structured output with strategy-based fallback."""
        request = LLMRequest(...)

        async def call_fn(provider, req) -> tuple[Any, LLMResponse]:
            return await provider.complete_structured(req, response_schema)

        return await self._execute_with_fallback(
            action, strategy, request, call_fn,
        )

    # -- internal: strategy fallback --------------------------------

    async def _execute_with_fallback(
        self, action, strategy, request, call_fn,
    ) -> Any:
        """Two-level fallback: requested strategy -> default."""
        # 1. Try requested strategy chain
        # 2. If failed and strategy != "default" → try default chain
        # 3. All failed → AllModelsFailedError
        # On cross-strategy fallback: strategy = "quality->default"

    # -- internal: chain iteration ----------------------------------

    async def _try_chain(
        self, action, strategy, request, call_fn, errors,
    ) -> Any | None:
        """Walk model chain. Sets request.model = model_cfg.model_id."""
        chain = self._registry.get_chain(action, strategy)
        for model_cfg in chain:
            provider = self._get_active_provider(model_cfg, errors)
            if provider is None:
                continue
            request_for_model = request.model_copy(
                update={"model": model_cfg.model_id},
            )
            result = await self._try_with_retries(...)
            if result is not None:
                return result
        return None

    def _get_active_provider(
        self, model_cfg: ModelConfig, errors,
    ) -> LLMProvider | None:
        """Check provider exists and is enabled."""

    # -- internal: retry loop ---------------------------------------

    async def _try_with_retries(
        self, provider, request, model_cfg, call_fn, errors, action, strategy,
    ) -> Any | None:
        """Retry call_fn up to max_attempts. Permanent errors → break."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                result = await call_fn(provider, request)
                self._enrich_response(result, model_cfg, action, strategy)
                await self._log_success(result)
                return result
            except Exception as exc:
                if not self._is_retryable(exc):
                    # 401, 403 etc — skip model immediately
                    break
                if attempt == self._max_attempts:
                    errors.append(...)
        return None

    # -- helpers ----------------------------------------------------

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Duck-typed error classification (no SDK imports).

        - StructuredOutputError → retryable (LLM may produce valid JSON)
        - exc.status_code in (400, 401, 403, 404) → permanent
        - exc.code in (400, 401, 403, 404) → permanent (google-genai)
        - Everything else → retryable (fail-safe default)
        """

    @staticmethod
    def _enrich_response(result, model_cfg, action, strategy) -> None:
        """Set action, strategy, cost_usd on LLMResponse."""

    @staticmethod
    def _set_strategy_path(result, strategy_path: str) -> None:
        """Set strategy on cross-strategy fallback."""

    async def _log_success(self, result) -> None: ...
    async def _log_failure(self, model_cfg, request, error) -> None: ...
    async def _log(self, response, *, success, error_message=None) -> None:
        """Structlog + optional LogCallback."""
```

---

## src/course_supporter/llm/__init__.py

```python
"""LLM infrastructure: providers, schemas, router, registry."""

from course_supporter.llm.router import AllModelsFailedError, ModelRouter
from course_supporter.llm.schemas import LLMRequest, LLMResponse

__all__ = ["AllModelsFailedError", "LLMRequest", "LLMResponse", "ModelRouter"]
```

---

## Тести

### tests/unit/test_llm/test_router.py

24 тести, `asyncio_mode = "auto"` (без `@pytest.mark.asyncio`):

| Клас | Тести |
|------|-------|
| `TestDefaultStrategy` | primary succeeds; fallback within chain |
| `TestExplicitStrategy` | quality chain order respected |
| `TestCrossStrategyFallback` | quality→default; default doesn't self-fallback |
| `TestDisabledProvider` | skip disabled |
| `TestMissingProvider` | provider not in dict → skip |
| `TestAllFail` | both strategies tried; error details populated |
| `TestCostEnrichment` | cost with tokens; cost None without tokens |
| `TestLogCallback` | callback called; success flag passed |
| `TestRetryBehavior` | retries up to max_attempts; retry then success |
| `TestPermanentError` | 401 → call_count=1 (no retries) |
| `TestCompleteStructured` | structured success; structured fallback |
| `TestModelIdPassedToProvider` | request.model == model_id from chain |
| `TestIsRetryable` | StructuredOutputError, 401, 429, 500, generic |

Test helpers використовують `ModelRegistryConfig.model_validate()` з dict-based конструкцією (відповідає реальній структурі `ModelConfig` з `CostPer1K`).

---

## Примітки

- **Cross-strategy fallback** — лише `requested → default`. Простий і передбачуваний.
- **`strategy="quality->default"`** — response.strategy показує фактичний шлях (ASCII arrow).
- **Disabled provider** — skip, не error. Runtime відключення без впливу на інші.
- **Missing provider** — якщо провайдер з registry не сконфігурований, skip з error "provider not configured".
- **LogCallback**: `Callable[[LLMResponse, bool, str | None], Awaitable[None]]`. Action/strategy вже в LLMResponse.
- **DRY**: `complete()` і `complete_structured()` — тонкі обгортки з `call_fn` closure, делегують `_execute_with_fallback`. 6 методів зі специфікації → 2 public + 1 fallback + 1 chain + 1 retry + helpers.
- **`max_attempts`** (не `max_retries`): `max_attempts=2` = 2 спроби (initial + 1 retry).
- **Error classification**: duck-typed через `getattr(exc, "status_code", None)` — без імпорту SDK-специфічних класів.
- **Auto-disable**: відкладено. Поточна реалізація логує permanent errors і пропускає модель, але не відключає провайдер автоматично.
