"""Tests for STT provider interface and factory."""

from pathlib import Path

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
        assert set(STT_PROVIDER_REGISTRY) == {"elevenlabs", "openai_stt"}

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

    def test_every_key_set_creates_exactly_the_chain_providers(self) -> None:
        """With every STT key set, the factory builds the two chain providers."""
        from course_supporter.config import Settings
        from course_supporter.stt.factory import create_stt_providers

        s = Settings(
            elevenlabs_api_key="test-key",  # type: ignore[arg-type]
            openai_api_key="test-key",  # type: ignore[arg-type]
            _env_file=None,
        )
        providers = create_stt_providers(s)
        assert set(providers) == {"elevenlabs", "openai_stt"}


class TestSttTimeoutFor:
    """Duration-proportional STT request timeout helper (task M1)."""

    def test_short_clip_clamps_to_floor(self) -> None:
        from course_supporter.stt.utils import _STT_TIMEOUT_FLOOR_SEC, stt_timeout_for

        # 1-min clip: budget 0.1 -> 6 s < FLOOR -> fail-fast preserved.
        assert stt_timeout_for(60.0) == _STT_TIMEOUT_FLOOR_SEC

    def test_contract_150min_scales_under_cap(self) -> None:
        from course_supporter.stt.utils import _STT_TIMEOUT_CAP_SEC, stt_timeout_for

        # 150-min (9000 s) x 0.1 = 900 s -> proportional, below CAP.
        result = stt_timeout_for(9000.0)
        assert result == pytest.approx(900.0)
        assert result < _STT_TIMEOUT_CAP_SEC

    def test_none_duration_falls_back_to_cap(self) -> None:
        from course_supporter.stt.utils import _STT_TIMEOUT_CAP_SEC, stt_timeout_for

        # Pure-audio path: duration unknown pre-STT -> conservative CAP.
        assert stt_timeout_for(None) == _STT_TIMEOUT_CAP_SEC


class TestSTTRequestTimeout:
    """Providers apply a per-request duration-proportional timeout (task M1)."""

    @pytest.fixture()
    def fake_audio(self, tmp_path: Path) -> Path:
        p = tmp_path / "sample.mp3"
        p.write_bytes(b"\xff\xfb\x00\x00")
        return p

    async def test_elevenlabs_none_duration_uses_cap(
        self, fake_audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from course_supporter.stt.providers.elevenlabs import ElevenLabsSTTProvider
        from course_supporter.stt.schemas import STTRequest
        from course_supporter.stt.utils import stt_timeout_for

        provider = ElevenLabsSTTProvider(api_keys=["k"])
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "text": "hi",
            "language_code": "eng",
            "words": [{"text": "hi", "start": 0.0, "end": 0.5}],
        }
        post_mock = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr(provider._clients[0], "post", post_mock)

        await provider.transcribe(STTRequest(audio_path=str(fake_audio)))

        assert post_mock.await_args.kwargs["timeout"].read == stt_timeout_for(None)

    async def test_elevenlabs_passes_duration_proportional_timeout(
        self, fake_audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from course_supporter.stt.providers.elevenlabs import ElevenLabsSTTProvider
        from course_supporter.stt.schemas import STTRequest
        from course_supporter.stt.utils import stt_timeout_for

        provider = ElevenLabsSTTProvider(api_keys=["k"])
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "text": "hi",
            "language_code": "eng",
            "words": [{"text": "hi", "start": 0.0, "end": 0.5}],
        }
        post_mock = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr(provider._clients[0], "post", post_mock)

        await provider.transcribe(
            STTRequest(audio_path=str(fake_audio), duration_sec=9000.0)
        )

        timeout = post_mock.await_args.kwargs["timeout"]
        assert timeout.read == stt_timeout_for(9000.0)
