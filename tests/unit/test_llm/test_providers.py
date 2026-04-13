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


class TestGeminiBuildContents:
    """_build_contents maps our generic format to Gemini SDK shapes."""

    def test_text_only_returns_prompt_string(self) -> None:
        from course_supporter.llm.providers.gemini import _build_contents

        req = LLMRequest(prompt="hello")
        assert _build_contents(req) == "hello"

    def test_empty_contents_returns_prompt(self) -> None:
        from course_supporter.llm.providers.gemini import _build_contents

        req = LLMRequest(prompt="hi", contents=[])
        assert _build_contents(req) == "hi"

    def test_raw_bytes_wrapped_into_parts_with_prompt(self) -> None:
        """VD path: bytes list + prompt text → one Content with Parts."""
        from google.genai import types

        from course_supporter.llm.providers.gemini import _build_contents

        # Valid JPEG magic bytes so MIME detection succeeds.
        img = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
        req = LLMRequest(prompt="Describe this", contents=[img])
        result = _build_contents(req)

        assert isinstance(result, list)
        assert len(result) == 1
        content = result[0]
        assert isinstance(content, types.Content)
        # Parts: 1 image + 1 text
        assert len(content.parts) == 2

    def test_multiple_images_plus_prompt(self) -> None:
        from google.genai import types

        from course_supporter.llm.providers.gemini import _build_contents

        jpeg = b"\xff\xd8\xff\xe0" + b"x" * 10
        req = LLMRequest(prompt="compare", contents=[jpeg, jpeg, jpeg])
        result = _build_contents(req)

        assert isinstance(result, list)
        assert len(result) == 1
        assert len(result[0].parts) == 4  # 3 images + 1 text
        for i in range(3):
            assert isinstance(result[0].parts[i], types.Part)

    def test_already_sdk_shaped_contents_passthrough(self) -> None:
        """Legacy caller passes Content/Part/str — must not rewrap into Parts."""
        from google.genai import types

        from course_supporter.llm.providers.gemini import _build_contents

        existing = [types.Content(parts=[types.Part.from_text(text="hi")])]
        req = LLMRequest(prompt="ignored", contents=existing)
        result = _build_contents(req)
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], types.Content)
        assert len(result[0].parts) == 1  # still the single "hi" text part

    def test_mixed_bytes_and_sdk_items_rejected(self) -> None:
        """Mixing bytes with SDK-native items is a bug — raise TypeError."""
        from google.genai import types

        from course_supporter.llm.providers.gemini import _build_contents

        req = LLMRequest(
            prompt="x",
            contents=[
                b"\xff\xd8\xff\xe0",
                types.Content(parts=[types.Part.from_text(text="hi")]),
            ],
        )
        with pytest.raises(TypeError, match="cannot mix raw bytes"):
            _build_contents(req)


class TestGeminiDetectImageMime:
    """Magic-byte MIME detection for image payloads."""

    def test_jpeg(self) -> None:
        from course_supporter.llm.providers.gemini import _detect_image_mime

        assert _detect_image_mime(b"\xff\xd8\xff\xe0JFIF...") == "image/jpeg"

    def test_png(self) -> None:
        from course_supporter.llm.providers.gemini import _detect_image_mime

        assert _detect_image_mime(b"\x89PNG\r\n\x1a\nIHDR...") == "image/png"

    def test_gif(self) -> None:
        from course_supporter.llm.providers.gemini import _detect_image_mime

        assert _detect_image_mime(b"GIF89a...") == "image/gif"
        assert _detect_image_mime(b"GIF87a...") == "image/gif"

    def test_webp(self) -> None:
        from course_supporter.llm.providers.gemini import _detect_image_mime

        # RIFF<4 bytes size>WEBP<...>
        data = b"RIFF\x00\x00\x00\x00WEBPVP8 ..."
        assert _detect_image_mime(data) == "image/webp"

    def test_unknown_falls_back_to_octet_stream(self) -> None:
        from course_supporter.llm.providers.gemini import _detect_image_mime

        assert _detect_image_mime(b"random nonsense") == "application/octet-stream"

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
