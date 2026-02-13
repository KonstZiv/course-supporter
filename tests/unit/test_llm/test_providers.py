"""Tests for LLM providers."""

import pytest
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider, StructuredOutputError
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


class TestParseStructured:
    """Verify _parse_structured helper and StructuredOutputError."""

    class _SimpleSchema(BaseModel):
        name: str
        value: int

    def _make_provider(self) -> LLMProvider:
        class DummyProvider(LLMProvider):
            provider_name = "dummy"

            async def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(content="", provider="dummy", model_id="d")

            async def complete_structured(self, request, schema):  # type: ignore[override]
                return None, LLMResponse(content="", provider="dummy", model_id="d")

        return DummyProvider()

    def test_parse_valid_json(self) -> None:
        p = self._make_provider()
        raw = '{"name": "test", "value": 42}'
        result = p._parse_structured(raw, self._SimpleSchema)
        assert result.name == "test"
        assert result.value == 42

    def test_parse_invalid_json_raises_structured_output_error(self) -> None:
        p = self._make_provider()
        with pytest.raises(StructuredOutputError) as exc_info:
            p._parse_structured("not valid json", self._SimpleSchema)

        err = exc_info.value
        assert err.provider == "dummy"
        assert err.schema_name == "_SimpleSchema"
        assert "not valid json" in err.raw_content
        assert err.__cause__ is not None

    def test_parse_wrong_schema_raises_structured_output_error(self) -> None:
        p = self._make_provider()
        with pytest.raises(StructuredOutputError):
            p._parse_structured('{"name": "test"}', self._SimpleSchema)


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
