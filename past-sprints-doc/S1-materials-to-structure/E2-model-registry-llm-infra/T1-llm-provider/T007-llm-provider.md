# 📋 S1-007: LLM Providers

## Мета

Створити розширюваний реєстр LLM-провайдерів: абстрактний інтерфейс `LLMProvider` + реалізації для Gemini, Anthropic, OpenAI, DeepSeek. Додати нового провайдера = написати клас + зареєструвати в конфігу. Провайдери підтримують runtime enable/disable (вичерпаний ліміт, API недоступний).

## Контекст

Залежить від S1-004 (config з API keys як SecretStr). Це нижній шар LLM-інфраструктури. Над ним — Actions & Registry (S1-008), ModelRouter (S1-009).

---

## Acceptance Criteria

- [ ] `LLMProvider` ABC визначає контракт: `complete()` та `complete_structured()`
- [ ] Реалізовано 3 класи провайдерів: `GeminiProvider`, `AnthropicProvider`, `OpenAICompatProvider`
- [ ] DeepSeek працює через `OpenAICompatProvider` з кастомним `base_url`
- [ ] Кожен провайдер повертає уніфікований `LLMResponse` з tokens/latency/model metadata
- [ ] Provider registry: додати провайдер = клас + запис у `PROVIDER_REGISTRY`, без зміни factory
- [ ] Runtime enable/disable: `provider.enabled` flag, disable при помилках / вичерпаному ліміті
- [ ] Unit-тести з мокнутими SDK — без реальних API-викликів

---

## Pydantic-схеми

### src/course_supporter/llm/schemas.py

```python
"""Shared schemas for LLM infrastructure."""

from datetime import datetime

from pydantic import BaseModel, Field


class LLMRequest(BaseModel):
    """Input for LLM call."""

    prompt: str
    system_prompt: str | None = None
    temperature: float = 0.0
    max_tokens: int = 4096
    action: str = ""           # video_analysis, course_structuring, ...
    strategy: str = "default"  # default, quality, budget


class LLMResponse(BaseModel):
    """Unified response from any LLM provider."""

    content: str
    provider: str          # gemini, anthropic, openai, deepseek
    model_id: str          # gemini-2.5-flash, claude-sonnet-4, ...
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0
    cost_usd: float | None = None
    action: str = ""
    strategy: str = "default"
    finished_at: datetime = Field(default_factory=datetime.now)
```

---

## Абстрактний інтерфейс

### src/course_supporter/llm/providers/base.py

```python
"""Abstract LLM provider interface."""

import abc
import time
from typing import Any

from pydantic import BaseModel

from course_supporter.llm.schemas import LLMRequest, LLMResponse


class LLMProvider(abc.ABC):
    """Base class for all LLM providers.

    Each provider implements two methods:
    - complete(): free-form text generation
    - complete_structured(): generation with Pydantic schema validation

    Providers support runtime enable/disable for handling
    rate limits, quota exhaustion, or API outages.
    """

    provider_name: str = ""

    def __init__(self) -> None:
        self._enabled: bool = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def disable(self, reason: str = "") -> None:
        """Disable provider at runtime (rate limit, API down, etc.)."""
        self._enabled = False

    def enable(self) -> None:
        """Re-enable provider."""
        self._enabled = True

    @abc.abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Generate text completion."""
        ...

    @abc.abstractmethod
    async def complete_structured(
        self,
        request: LLMRequest,
        response_schema: type[BaseModel],
    ) -> tuple[Any, LLMResponse]:
        """Generate structured output validated against Pydantic schema.

        Returns:
            Tuple of (parsed_object, raw_llm_response).
            parsed_object is an instance of response_schema.
        """
        ...

    def _measure_latency(self) -> "_LatencyTimer":
        """Context manager for measuring call latency."""
        return _LatencyTimer()


class _LatencyTimer:
    """Simple latency measurement helper."""

    def __init__(self) -> None:
        self.start: float = 0
        self.elapsed_ms: int = 0

    def __enter__(self) -> "_LatencyTimer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args: object) -> None:
        self.elapsed_ms = int((time.perf_counter() - self.start) * 1000)
```

---

## Реалізації провайдерів

### src/course_supporter/llm/providers/gemini.py

```python
"""Google Gemini provider via google-genai SDK."""

import json
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse


class GeminiProvider(LLMProvider):
    """Gemini provider using google-genai SDK.

    Supports text generation and structured output via
    response_mime_type="application/json" + response_schema.
    """

    provider_name = "gemini"

    def __init__(self, api_key: str, default_model: str = "gemini-2.5-flash") -> None:
        super().__init__()
        self._client = genai.Client(api_key=api_key)
        self._default_model = default_model

    async def complete(self, request: LLMRequest) -> LLMResponse:
        config = types.GenerateContentConfig(
            temperature=request.temperature,
            max_output_tokens=request.max_tokens,
            system_instruction=request.system_prompt,
        )

        with self._measure_latency() as timer:
            response = await self._client.aio.models.generate_content(
                model=self._default_model,
                contents=request.prompt,
                config=config,
            )

        usage = response.usage_metadata
        return LLMResponse(
            content=response.text or "",
            provider=self.provider_name,
            model_id=self._default_model,
            tokens_in=usage.prompt_token_count if usage else None,
            tokens_out=usage.candidates_token_count if usage else None,
            latency_ms=timer.elapsed_ms,
        )

    async def complete_structured(
        self,
        request: LLMRequest,
        response_schema: type[BaseModel],
    ) -> tuple[Any, LLMResponse]:
        config = types.GenerateContentConfig(
            temperature=request.temperature,
            max_output_tokens=request.max_tokens,
            system_instruction=request.system_prompt,
            response_mime_type="application/json",
            response_schema=response_schema,
        )

        with self._measure_latency() as timer:
            response = await self._client.aio.models.generate_content(
                model=self._default_model,
                contents=request.prompt,
                config=config,
            )

        usage = response.usage_metadata
        llm_response = LLMResponse(
            content=response.text or "",
            provider=self.provider_name,
            model_id=self._default_model,
            tokens_in=usage.prompt_token_count if usage else None,
            tokens_out=usage.candidates_token_count if usage else None,
            latency_ms=timer.elapsed_ms,
        )

        parsed = response_schema.model_validate_json(response.text or "{}")
        return parsed, llm_response
```

### src/course_supporter/llm/providers/anthropic.py

```python
"""Anthropic Claude provider."""

import json
from typing import Any

import anthropic
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse


class AnthropicProvider(LLMProvider):
    """Anthropic provider using official SDK."""

    provider_name = "anthropic"

    def __init__(
        self, api_key: str, default_model: str = "claude-sonnet-4-20250514"
    ) -> None:
        super().__init__()
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._default_model = default_model

    async def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._default_model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt
        if request.temperature > 0:
            kwargs["temperature"] = request.temperature

        with self._measure_latency() as timer:
            response = await self._client.messages.create(**kwargs)

        return LLMResponse(
            content=response.content[0].text if response.content else "",
            provider=self.provider_name,
            model_id=self._default_model,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            latency_ms=timer.elapsed_ms,
        )

    async def complete_structured(
        self,
        request: LLMRequest,
        response_schema: type[BaseModel],
    ) -> tuple[Any, LLMResponse]:
        schema_json = json.dumps(
            response_schema.model_json_schema(), ensure_ascii=False
        )
        structured_system = (
            f"{request.system_prompt or ''}\n\n"
            f"Respond ONLY with valid JSON matching this schema:\n{schema_json}"
        )
        modified_request = request.model_copy(
            update={"system_prompt": structured_system}
        )
        llm_response = await self.complete(modified_request)
        parsed = response_schema.model_validate_json(llm_response.content)
        return parsed, llm_response
```

### src/course_supporter/llm/providers/openai_compat.py

```python
"""OpenAI-compatible provider (OpenAI + DeepSeek)."""

import json
from typing import Any

import openai
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse


class OpenAICompatProvider(LLMProvider):
    """Provider for OpenAI API and compatible services (DeepSeek).

    DeepSeek uses the same API format with a different base_url.
    """

    def __init__(
        self,
        api_key: str,
        provider_name: str = "openai",
        default_model: str = "gpt-4o-mini",
        base_url: str | None = None,
    ) -> None:
        super().__init__()
        self.provider_name = provider_name
        self._default_model = default_model
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        with self._measure_latency() as timer:
            response = await self._client.chat.completions.create(
                model=self._default_model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )

        choice = response.choices[0]
        usage = response.usage
        return LLMResponse(
            content=choice.message.content or "",
            provider=self.provider_name,
            model_id=self._default_model,
            tokens_in=usage.prompt_tokens if usage else None,
            tokens_out=usage.completion_tokens if usage else None,
            latency_ms=timer.elapsed_ms,
        )

    async def complete_structured(
        self,
        request: LLMRequest,
        response_schema: type[BaseModel],
    ) -> tuple[Any, LLMResponse]:
        messages: list[dict[str, str]] = []
        schema_json = json.dumps(
            response_schema.model_json_schema(), ensure_ascii=False
        )
        system = (
            f"{request.system_prompt or ''}\n\n"
            f"Respond ONLY with valid JSON matching this schema:\n{schema_json}"
        )
        messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": request.prompt})

        with self._measure_latency() as timer:
            response = await self._client.chat.completions.create(
                model=self._default_model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                response_format={"type": "json_object"},
            )

        choice = response.choices[0]
        usage = response.usage
        llm_response = LLMResponse(
            content=choice.message.content or "",
            provider=self.provider_name,
            model_id=self._default_model,
            tokens_in=usage.prompt_tokens if usage else None,
            tokens_out=usage.completion_tokens if usage else None,
            latency_ms=timer.elapsed_ms,
        )
        parsed = response_schema.model_validate_json(llm_response.content)
        return parsed, llm_response
```

---

## Provider Registry (розширюваність)

### src/course_supporter/llm/providers/__init__.py

```python
"""LLM provider implementations.

PROVIDER_REGISTRY maps provider names (used in models.yaml)
to their implementation classes. To add a new provider:

1. Create a new module in this package (e.g., mistral.py)
2. Implement LLMProvider subclass
3. Add entry to PROVIDER_REGISTRY below

That's it — no changes to factory.py or router.py needed.
"""

from course_supporter.llm.providers.anthropic import AnthropicProvider
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.providers.gemini import GeminiProvider
from course_supporter.llm.providers.openai_compat import OpenAICompatProvider

PROVIDER_REGISTRY: dict[str, type[LLMProvider]] = {
    "gemini": GeminiProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAICompatProvider,
    "deepseek": OpenAICompatProvider,
}

__all__ = [
    "AnthropicProvider",
    "GeminiProvider",
    "LLMProvider",
    "OpenAICompatProvider",
    "PROVIDER_REGISTRY",
]
```

### src/course_supporter/llm/factory.py

```python
"""Provider factory — creates providers based on available API keys.

Uses PROVIDER_REGISTRY for extensibility. Adding a new provider
requires NO changes to this file.
"""

import structlog

from course_supporter.config import Settings
from course_supporter.llm.providers import PROVIDER_REGISTRY, LLMProvider

logger = structlog.get_logger()

# Маппінг provider_name → config для створення інстансу
PROVIDER_CONFIG: dict[str, dict] = {
    "gemini": {
        "key_attr": "gemini_api_key",
    },
    "anthropic": {
        "key_attr": "anthropic_api_key",
    },
    "openai": {
        "key_attr": "openai_api_key",
    },
    "deepseek": {
        "key_attr": "deepseek_api_key",
        "extra_kwargs": {
            "provider_name": "deepseek",
            "default_model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
        },
    },
}


def create_providers(settings: Settings) -> dict[str, LLMProvider]:
    """Instantiate providers for all configured API keys.

    Returns dict: provider_name -> LLMProvider instance.
    Only providers with non-None API keys are created.
    """
    providers: dict[str, LLMProvider] = {}

    for name, provider_cls in PROVIDER_REGISTRY.items():
        config = PROVIDER_CONFIG.get(name, {})
        key_attr = config.get("key_attr")
        if not key_attr:
            continue

        api_key_secret = getattr(settings, key_attr, None)
        if api_key_secret is None:
            continue

        kwargs: dict = {"api_key": api_key_secret.get_secret_value()}
        kwargs.update(config.get("extra_kwargs", {}))

        providers[name] = provider_cls(**kwargs)
        logger.info("llm_provider_registered", provider=name)

    if not providers:
        logger.warning("no_llm_providers_configured")

    return providers
```

---

## Тести

### tests/unit/test_llm/test_providers.py

```python
"""Tests for LLM providers."""

import pytest
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse


class TestLLMProviderInterface:
    """Verify LLMProvider ABC contract."""

    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError):
            LLMProvider()  # type: ignore[abstract]

    def test_subclass_must_implement_methods(self) -> None:
        class IncompleteProvider(LLMProvider):
            provider_name = "incomplete"

        with pytest.raises(TypeError):
            IncompleteProvider()  # type: ignore[abstract]

    def test_enabled_by_default(self) -> None:
        class DummyProvider(LLMProvider):
            provider_name = "dummy"

            async def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(content="", provider="dummy", model_id="d")

            async def complete_structured(self, request, schema):  # type: ignore[override]
                return None, LLMResponse(content="", provider="dummy", model_id="d")

        p = DummyProvider()
        assert p.enabled is True

    def test_disable_enable(self) -> None:
        class DummyProvider(LLMProvider):
            provider_name = "dummy"

            async def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(content="", provider="dummy", model_id="d")

            async def complete_structured(self, request, schema):  # type: ignore[override]
                return None, LLMResponse(content="", provider="dummy", model_id="d")

        p = DummyProvider()
        p.disable(reason="rate limit")
        assert p.enabled is False
        p.enable()
        assert p.enabled is True


class TestProviderRegistry:
    def test_registry_contains_all_providers(self) -> None:
        from course_supporter.llm.providers import PROVIDER_REGISTRY

        assert "gemini" in PROVIDER_REGISTRY
        assert "anthropic" in PROVIDER_REGISTRY
        assert "openai" in PROVIDER_REGISTRY
        assert "deepseek" in PROVIDER_REGISTRY

    def test_registry_values_are_provider_subclasses(self) -> None:
        from course_supporter.llm.providers import PROVIDER_REGISTRY

        for name, cls in PROVIDER_REGISTRY.items():
            assert issubclass(cls, LLMProvider), f"{name} is not LLMProvider subclass"


class TestProviderFactory:
    def test_no_keys_returns_empty(self) -> None:
        from course_supporter.config import Settings
        from course_supporter.llm.factory import create_providers

        s = Settings(_env_file=None)
        providers = create_providers(s)
        assert len(providers) == 0

    def test_gemini_key_creates_provider(self) -> None:
        from course_supporter.config import Settings
        from course_supporter.llm.factory import create_providers
        from course_supporter.llm.providers.gemini import GeminiProvider

        s = Settings(gemini_api_key="test-key", _env_file=None)  # type: ignore[arg-type]
        providers = create_providers(s)
        assert "gemini" in providers
        assert isinstance(providers["gemini"], GeminiProvider)

    def test_deepseek_uses_openai_compat(self) -> None:
        from course_supporter.config import Settings
        from course_supporter.llm.factory import create_providers
        from course_supporter.llm.providers.openai_compat import OpenAICompatProvider

        s = Settings(deepseek_api_key="test-key", _env_file=None)  # type: ignore[arg-type]
        providers = create_providers(s)
        assert "deepseek" in providers
        assert isinstance(providers["deepseek"], OpenAICompatProvider)
        assert providers["deepseek"].provider_name == "deepseek"


class TestLLMResponseModel:
    def test_response_defaults(self) -> None:
        r = LLMResponse(content="hello", provider="test", model_id="test-model")
        assert r.tokens_in is None
        assert r.latency_ms == 0
        assert r.strategy == "default"

    def test_response_with_all_fields(self) -> None:
        r = LLMResponse(
            content="hello",
            provider="gemini",
            model_id="gemini-2.5-flash",
            tokens_in=100,
            tokens_out=50,
            latency_ms=350,
            cost_usd=0.001,
            action="video_analysis",
            strategy="quality",
        )
        assert r.action == "video_analysis"
        assert r.strategy == "quality"
```

---

## Структура файлів

```
src/course_supporter/llm/
├── __init__.py
├── schemas.py                # LLMRequest, LLMResponse
├── factory.py                # create_providers() — data-driven, extensible
└── providers/
    ├── __init__.py            # PROVIDER_REGISTRY
    ├── base.py                # LLMProvider ABC + enable/disable
    ├── gemini.py              # GeminiProvider
    ├── anthropic.py           # AnthropicProvider
    └── openai_compat.py       # OpenAICompatProvider (OpenAI + DeepSeek)
```

---

## Кроки виконання

1. Створити `llm/schemas.py` (LLMRequest з action/strategy, LLMResponse)
2. Створити `llm/providers/base.py` (ABC з enable/disable)
3. Реалізувати `GeminiProvider`, `AnthropicProvider`, `OpenAICompatProvider`
4. Створити `providers/__init__.py` з `PROVIDER_REGISTRY`
5. Створити `factory.py` — data-driven create_providers
6. Створити `tests/unit/test_llm/test_providers.py`
7. `make check`

---

## Примітки

- **PROVIDER_REGISTRY** — dict замість if/elif ланцюжка. Додати Mistral, Groq, або будь-який OpenAI-compatible = один клас + один рядок у registry.
- **PROVIDER_CONFIG** — окремо від registry, бо містить secrets mapping. Це config-специфічна логіка, не потрібна в registry.
- **enable/disable** — простий boolean flag. ModelRouter (S1-009) перевіряє `provider.enabled` перед викликом. Можна розширити до circuit breaker — але для MVP достатньо manual disable.
