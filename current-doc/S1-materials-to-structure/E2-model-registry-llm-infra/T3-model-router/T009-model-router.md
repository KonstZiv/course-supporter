# 📋 S1-009: ModelRouter

## Мета

Центральна точка для всіх LLM-викликів. Приймає action + prompt + optional strategy, обирає chain моделей, обробляє retry/fallback двох рівнів: всередині chain (модель → модель) та між strategies (requested chain впав → default chain). Перевіряє `provider.enabled` перед викликом.

## Контекст

Залежить від S1-007 (providers з enable/disable) та S1-008 (registry з strategies). Використовується усіма наступними компонентами.

---

## Acceptance Criteria

- [ ] `router.complete(action, prompt)` — default strategy, fallback всередині chain
- [ ] `router.complete(action, prompt, strategy="quality")` — explicit strategy
- [ ] Disabled provider → skip, try next в chain
- [ ] Весь requested chain впав → fallback на `default` strategy (якщо не вже default)
- [ ] Всі strategies вичерпані → `AllModelsFailedError` з деталями
- [ ] Retry до max_retries на кожну модель
- [ ] Cost enrichment — автоматичний `cost_usd`
- [ ] LogCallback для запису в DB (S1-010)
- [ ] action/strategy проставляються в LLMResponse

---

## src/course_supporter/llm/router.py

```python
"""ModelRouter — central entry point for all LLM calls.

Two-level fallback:
1. Within chain: model 1 → model 2 → model 3
2. Between strategies: quality chain failed → fallback to default chain
"""

import structlog
from typing import Any, Callable, Awaitable

from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import ModelRegistryConfig, ModelCapability
from course_supporter.llm.schemas import LLMRequest, LLMResponse

logger = structlog.get_logger()

LogCallback = Callable[[LLMResponse, bool, str | None], Awaitable[None]]


class AllModelsFailedError(Exception):
    """All models in all attempted strategies failed."""

    def __init__(
        self, action: str, strategies_tried: list[str],
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
    """Routes LLM requests with strategy-based fallback.

    Fallback order:
    1. Try each model in the requested strategy's chain
    2. If all fail AND strategy != "default" → try default chain
    3. If all fail → AllModelsFailedError
    """

    def __init__(
        self,
        providers: dict[str, LLMProvider],
        registry: ModelRegistryConfig,
        log_callback: LogCallback | None = None,
        max_retries: int = 2,
    ) -> None:
        self._providers = providers
        self._registry = registry
        self._log_callback = log_callback
        self._max_retries = max_retries

    async def complete(
        self,
        action: str,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        strategy: str = "default",
    ) -> LLMResponse:
        """Generate text completion with strategy-based fallback."""
        request = LLMRequest(
            prompt=prompt, system_prompt=system_prompt,
            temperature=temperature, max_tokens=max_tokens,
            action=action, strategy=strategy,
        )

        errors: list[tuple[str, str]] = []
        strategies_tried: list[str] = []

        # 1. Try requested strategy
        response = await self._try_chain(action, strategy, request, errors)
        strategies_tried.append(strategy)
        if response is not None:
            return response

        # 2. Fallback to default (if not already default)
        if strategy != "default":
            logger.info(
                "strategy_chain_exhausted_falling_back",
                action=action, failed_strategy=strategy,
                fallback_strategy="default",
            )
            response = await self._try_chain(action, "default", request, errors)
            strategies_tried.append("default")
            if response is not None:
                response.strategy = f"{strategy}→default"
                return response

        raise AllModelsFailedError(action, strategies_tried, errors)

    async def complete_structured(
        self,
        action: str,
        prompt: str,
        response_schema: type[BaseModel],
        *,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        strategy: str = "default",
    ) -> tuple[Any, LLMResponse]:
        """Generate structured output with strategy-based fallback."""
        request = LLMRequest(
            prompt=prompt, system_prompt=system_prompt,
            temperature=temperature, max_tokens=max_tokens,
            action=action, strategy=strategy,
        )

        errors: list[tuple[str, str]] = []
        strategies_tried: list[str] = []

        result = await self._try_chain_structured(
            action, strategy, request, response_schema, errors,
        )
        strategies_tried.append(strategy)
        if result is not None:
            return result

        if strategy != "default":
            result = await self._try_chain_structured(
                action, "default", request, response_schema, errors,
            )
            strategies_tried.append("default")
            if result is not None:
                parsed, response = result
                response.strategy = f"{strategy}→default"
                return parsed, response

        raise AllModelsFailedError(action, strategies_tried, errors)

    # ── internal ──────────────────────────────────────────

    async def _try_chain(
        self, action: str, strategy: str,
        request: LLMRequest, errors: list[tuple[str, str]],
    ) -> LLMResponse | None:
        chain = self._registry.get_chain(action, strategy)
        for model_cap in chain:
            provider = self._get_active_provider(model_cap, errors)
            if provider is None:
                continue
            response = await self._try_with_retries(
                provider, request, model_cap, errors,
            )
            if response is not None:
                response.action = action
                response.strategy = strategy
                return response
        return None

    async def _try_chain_structured(
        self, action: str, strategy: str,
        request: LLMRequest, response_schema: type[BaseModel],
        errors: list[tuple[str, str]],
    ) -> tuple[Any, LLMResponse] | None:
        chain = self._registry.get_chain(action, strategy)
        for model_cap in chain:
            provider = self._get_active_provider(model_cap, errors)
            if provider is None:
                continue
            result = await self._try_structured_with_retries(
                provider, request, response_schema, model_cap, errors,
            )
            if result is not None:
                parsed, response = result
                response.action = action
                response.strategy = strategy
                return parsed, response
        return None

    def _get_active_provider(
        self, model_cap: ModelCapability, errors: list[tuple[str, str]],
    ) -> LLMProvider | None:
        provider = self._providers.get(model_cap.provider)
        if provider is None:
            errors.append((model_cap.model_id, "provider not configured"))
            return None
        if not provider.enabled:
            errors.append((model_cap.model_id, "provider disabled"))
            return None
        return provider

    async def _try_with_retries(
        self, provider: LLMProvider, request: LLMRequest,
        model_cap: ModelCapability, errors: list[tuple[str, str]],
    ) -> LLMResponse | None:
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await provider.complete(request)
                response = self._enrich(response, model_cap)
                await self._log(response, success=True)
                return response
            except Exception as exc:
                logger.warning(
                    "llm_call_failed", provider=model_cap.provider,
                    model=model_cap.model_id, attempt=attempt, error=str(exc),
                )
                if attempt == self._max_retries:
                    errors.append((model_cap.model_id, str(exc)))
                    await self._log_failure(model_cap, request, str(exc))
        return None

    async def _try_structured_with_retries(
        self, provider: LLMProvider, request: LLMRequest,
        response_schema: type[BaseModel], model_cap: ModelCapability,
        errors: list[tuple[str, str]],
    ) -> tuple[Any, LLMResponse] | None:
        for attempt in range(1, self._max_retries + 1):
            try:
                parsed, response = await provider.complete_structured(
                    request, response_schema,
                )
                response = self._enrich(response, model_cap)
                await self._log(response, success=True)
                return parsed, response
            except Exception as exc:
                logger.warning(
                    "llm_structured_call_failed", provider=model_cap.provider,
                    model=model_cap.model_id, attempt=attempt, error=str(exc),
                )
                if attempt == self._max_retries:
                    errors.append((model_cap.model_id, str(exc)))
                    await self._log_failure(model_cap, request, str(exc))
        return None

    def _enrich(self, response: LLMResponse, model_cap: ModelCapability) -> LLMResponse:
        if response.tokens_in is not None and response.tokens_out is not None:
            response.cost_usd = model_cap.estimate_cost(
                response.tokens_in, response.tokens_out,
            )
        return response

    async def _log(
        self, response: LLMResponse, *, success: bool,
        error_message: str | None = None,
    ) -> None:
        if self._log_callback:
            await self._log_callback(response, success, error_message)
        logger.info(
            "llm_call_completed",
            provider=response.provider, model=response.model_id,
            action=response.action, strategy=response.strategy,
            tokens_in=response.tokens_in, latency_ms=response.latency_ms,
            cost_usd=response.cost_usd, success=success,
        )

    async def _log_failure(
        self, model_cap: ModelCapability, request: LLMRequest, error_message: str,
    ) -> None:
        dummy = LLMResponse(
            content="", provider=model_cap.provider, model_id=model_cap.model_id,
            action=request.action, strategy=request.strategy,
        )
        await self._log(dummy, success=False, error_message=error_message)
```

---

## src/course_supporter/llm/__init__.py

```python
"""LLM infrastructure — ModelRouter, providers, registry."""

from course_supporter.llm.router import AllModelsFailedError, ModelRouter
from course_supporter.llm.schemas import LLMRequest, LLMResponse

__all__ = ["AllModelsFailedError", "LLMRequest", "LLMResponse", "ModelRouter"]
```

---

## Тести

### tests/unit/test_llm/test_router.py

```python
"""Tests for ModelRouter — strategies, fallback, disabled providers."""

from unittest.mock import AsyncMock
import pytest

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import (
    ActionConfig, ModelCapability, ModelRegistryConfig,
)
from course_supporter.llm.router import AllModelsFailedError, ModelRouter
from course_supporter.llm.schemas import LLMResponse


def _resp(provider="p_a", model="a"):
    return LLMResponse(content="ok", provider=provider, model_id=model,
                       tokens_in=10, tokens_out=20, latency_ms=100)

def _registry(strategies=None):
    return ModelRegistryConfig(
        models={
            "model-a": ModelCapability(provider="p_a", model_id="a",
                capabilities=["structured_output"],
                cost_per_1k_input=0.001, cost_per_1k_output=0.002),
            "model-b": ModelCapability(provider="p_b", model_id="b",
                capabilities=["structured_output"]),
        },
        actions={"act": ActionConfig(requires=["structured_output"])},
        routing={"act": strategies or {
            "default": ["model-a", "model-b"],
            "quality": ["model-b", "model-a"],
            "budget": ["model-a"],
        }},
    )

def _ok(response=None):
    p = AsyncMock(spec=LLMProvider); p.complete = AsyncMock(return_value=response or _resp()); p.enabled = True; return p

def _fail():
    p = AsyncMock(spec=LLMProvider); p.complete = AsyncMock(side_effect=Exception("API error")); p.enabled = True; return p

def _disabled():
    p = AsyncMock(spec=LLMProvider); p.enabled = False; return p


class TestDefaultStrategy:
    @pytest.mark.asyncio
    async def test_primary_succeeds(self):
        r = await ModelRouter({"p_a": _ok(_resp("p_a", "a")), "p_b": _ok()}, _registry()).complete("act", "hi")
        assert r.provider == "p_a"

    @pytest.mark.asyncio
    async def test_fallback_within_chain(self):
        r = await ModelRouter({"p_a": _fail(), "p_b": _ok(_resp("p_b", "b"))}, _registry(), max_retries=1).complete("act", "hi")
        assert r.provider == "p_b"

class TestExplicitStrategy:
    @pytest.mark.asyncio
    async def test_quality_chain(self):
        r = await ModelRouter({"p_a": _ok(_resp("p_a")), "p_b": _ok(_resp("p_b", "b"))}, _registry()).complete("act", "hi", strategy="quality")
        assert r.provider == "p_b"
        assert r.strategy == "quality"

class TestCrossStrategyFallback:
    @pytest.mark.asyncio
    async def test_quality_to_default(self):
        r = await ModelRouter({"p_a": _ok(_resp("p_a", "a")), "p_b": _fail()}, _registry(), max_retries=1).complete("act", "hi", strategy="quality")
        assert r.provider == "p_a"
        assert "default" in r.strategy

    @pytest.mark.asyncio
    async def test_no_self_fallback(self):
        with pytest.raises(AllModelsFailedError) as exc:
            await ModelRouter({"p_a": _fail(), "p_b": _fail()}, _registry(), max_retries=1).complete("act", "hi")
        assert len(exc.value.strategies_tried) == 1

class TestDisabledProvider:
    @pytest.mark.asyncio
    async def test_skip(self):
        r = await ModelRouter({"p_a": _disabled(), "p_b": _ok(_resp("p_b"))}, _registry()).complete("act", "hi")
        assert r.provider == "p_b"

class TestAllFail:
    @pytest.mark.asyncio
    async def test_both_strategies_tried(self):
        with pytest.raises(AllModelsFailedError) as exc:
            await ModelRouter({"p_a": _fail(), "p_b": _fail()}, _registry(), max_retries=1).complete("act", "hi", strategy="budget")
        assert "budget" in exc.value.strategies_tried
        assert "default" in exc.value.strategies_tried

class TestCostAndLogging:
    @pytest.mark.asyncio
    async def test_cost(self):
        r = _resp(); r.tokens_in = 1000; r.tokens_out = 500
        assert (await ModelRouter({"p_a": _ok(r)}, _registry()).complete("act", "hi")).cost_usd > 0

    @pytest.mark.asyncio
    async def test_callback(self):
        cb = AsyncMock()
        await ModelRouter({"p_a": _ok()}, _registry(), log_callback=cb).complete("act", "hi")
        cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries(self):
        primary = _fail()
        await ModelRouter({"p_a": primary, "p_b": _ok(_resp("p_b"))}, _registry(), max_retries=3).complete("act", "hi")
        assert primary.complete.call_count == 3
```

---

## Примітки

- **Cross-strategy fallback** — лише `requested → default`. Простий і передбачуваний.
- **`strategy="quality→default"`** — response.strategy показує фактичний шлях для дебагу та аналітики.
- **Disabled provider** — skip, не error. Runtime відключення без впливу на інші.
- **LogCallback**: `(LLMResponse, bool, str | None)`. Action/strategy вже в LLMResponse.
