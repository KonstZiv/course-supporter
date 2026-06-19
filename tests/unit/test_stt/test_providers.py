"""Tests for STT provider interface and factory."""

from pathlib import Path
from typing import Any

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


class TestDeepgramLanguageAutoDetect:
    """Deepgram must auto-detect language when caller omits it."""

    @pytest.fixture()
    def fake_audio(self, tmp_path: Path) -> Path:
        p = tmp_path / "sample.wav"
        p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        return p

    def _response_body(
        self,
        *,
        detected_language: str | None = "uk",
        transcript: str = "hello",
        duration: float = 1.23,
    ) -> dict[str, Any]:
        """Build a Deepgram-shaped response body.

        Returned as a plain dict (not ``MagicMock``) on purpose: this mirrors
        Deepgram's real JSON contract so parser changes / API schema drift
        are caught by the test instead of being silently absorbed by
        attribute-auto-generating mocks.
        """
        channel: dict[str, Any] = {
            "alternatives": [
                {
                    "transcript": transcript,
                    "confidence": 0.95,
                    "paragraphs": {
                        "paragraphs": [
                            {
                                "sentences": [
                                    {"text": transcript, "start": 0.0, "end": 0.5}
                                ]
                            }
                        ]
                    },
                }
            ],
        }
        if detected_language is not None:
            channel["detected_language"] = detected_language
        return {
            "results": {"channels": [channel]},
            "metadata": {"duration": duration},
        }

    async def test_adds_detect_language_when_no_language(
        self, fake_audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When request.language=None, provider sends detect_language=true."""
        from unittest.mock import AsyncMock, MagicMock

        from course_supporter.stt.providers.deepgram import DeepgramSTTProvider
        from course_supporter.stt.schemas import STTRequest

        provider = DeepgramSTTProvider(api_keys=["k"])
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = self._response_body()
        post_mock = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr(provider._clients[0], "post", post_mock)

        result = await provider.transcribe(
            STTRequest(audio_path=str(fake_audio), language=None)
        )

        call_kwargs = post_mock.await_args.kwargs
        params = call_kwargs["params"]
        assert params.get("detect_language") is True
        assert "language" not in params
        assert result.detected_language == "uk"
        assert result.language is None

    async def test_no_detect_language_when_language_explicit(
        self, fake_audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit request.language → skip detect_language and pass it as-is."""
        from unittest.mock import AsyncMock, MagicMock

        from course_supporter.stt.providers.deepgram import DeepgramSTTProvider
        from course_supporter.stt.schemas import STTRequest

        provider = DeepgramSTTProvider(api_keys=["k"])
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = self._response_body(detected_language=None)
        post_mock = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr(provider._clients[0], "post", post_mock)

        result = await provider.transcribe(
            STTRequest(audio_path=str(fake_audio), language="en")
        )

        params = post_mock.await_args.kwargs["params"]
        assert params.get("language") == "en"
        assert "detect_language" not in params
        assert result.detected_language is None
        assert result.language == "en"


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

    async def test_deepgram_passes_duration_proportional_timeout(
        self, fake_audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from course_supporter.stt.providers.deepgram import DeepgramSTTProvider
        from course_supporter.stt.schemas import STTRequest
        from course_supporter.stt.utils import stt_timeout_for

        provider = DeepgramSTTProvider(api_keys=["k"])
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "results": {"channels": [{"alternatives": [{"transcript": "hi"}]}]},
            "metadata": {"duration": 1.0},
        }
        post_mock = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr(provider._clients[0], "post", post_mock)

        await provider.transcribe(
            STTRequest(audio_path=str(fake_audio), duration_sec=9000.0)
        )

        timeout = post_mock.await_args.kwargs["timeout"]
        assert timeout.read == stt_timeout_for(9000.0)

    async def test_deepgram_none_duration_uses_cap(
        self, fake_audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from course_supporter.stt.providers.deepgram import DeepgramSTTProvider
        from course_supporter.stt.schemas import STTRequest
        from course_supporter.stt.utils import stt_timeout_for

        provider = DeepgramSTTProvider(api_keys=["k"])
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "results": {"channels": [{"alternatives": [{"transcript": "hi"}]}]},
            "metadata": {"duration": 1.0},
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
