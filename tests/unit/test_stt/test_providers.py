"""Tests for STT provider interface and factory."""

import pytest

from course_supporter.stt.providers import STT_PROVIDER_REGISTRY
from course_supporter.stt.providers.base import STTProvider


class TestSTTProviderInterface:
    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError):
            STTProvider()  # type: ignore[abstract]

    def test_enabled_by_default(self) -> None:
        class Stub(STTProvider):
            async def transcribe(self, request):  # type: ignore[override]
                pass

        p = Stub()
        assert p.enabled is True

    def test_disable_enable(self) -> None:
        class Stub(STTProvider):
            async def transcribe(self, request):  # type: ignore[override]
                pass

        p = Stub()
        p.disable("rate limit")
        assert p.enabled is False
        p.enable()
        assert p.enabled is True


class TestSTTProviderRegistry:
    def test_registry_contains_all_providers(self) -> None:
        assert "elevenlabs" in STT_PROVIDER_REGISTRY
        assert "openai_stt" in STT_PROVIDER_REGISTRY
        assert "deepgram" in STT_PROVIDER_REGISTRY

    def test_registry_values_are_provider_subclasses(self) -> None:
        for cls in STT_PROVIDER_REGISTRY.values():
            assert issubclass(cls, STTProvider)


class TestSTTProviderFactory:
    def test_no_keys_returns_empty(self) -> None:
        from course_supporter.config import Settings
        from course_supporter.stt.factory import create_stt_providers

        s = Settings(_env_file=None)
        providers = create_stt_providers(s)
        assert len(providers) == 0

    def test_elevenlabs_key_creates_provider(self) -> None:
        from course_supporter.config import Settings
        from course_supporter.stt.factory import create_stt_providers
        from course_supporter.stt.providers.elevenlabs import ElevenLabsSTTProvider

        s = Settings(elevenlabs_api_key="test-key", _env_file=None)  # type: ignore[arg-type]
        providers = create_stt_providers(s)
        assert "elevenlabs" in providers
        assert isinstance(providers["elevenlabs"], ElevenLabsSTTProvider)

    def test_openai_stt_uses_openai_key(self) -> None:
        """openai_stt provider reuses the openai key pool."""
        from course_supporter.config import Settings
        from course_supporter.stt.factory import create_stt_providers
        from course_supporter.stt.providers.openai_stt import OpenAISTTProvider

        s = Settings(openai_api_key="test-key", _env_file=None)  # type: ignore[arg-type]
        providers = create_stt_providers(s)
        assert "openai_stt" in providers
        assert isinstance(providers["openai_stt"], OpenAISTTProvider)

    def test_deepgram_key_creates_provider(self) -> None:
        from course_supporter.config import Settings
        from course_supporter.stt.factory import create_stt_providers
        from course_supporter.stt.providers.deepgram import DeepgramSTTProvider

        s = Settings(deepgram_api_key="test-key", _env_file=None)  # type: ignore[arg-type]
        providers = create_stt_providers(s)
        assert "deepgram" in providers
        assert isinstance(providers["deepgram"], DeepgramSTTProvider)
