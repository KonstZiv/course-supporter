"""Tests for heavy steps factory."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from course_supporter.ingestion.audio import AudioProcessor
from course_supporter.ingestion.factory import (
    HeavySteps,
    create_heavy_steps,
    create_processors,
)
from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.ingestion.text import TextProcessor
from course_supporter.ingestion.video import VideoProcessor, WhisperVideoProcessor
from course_supporter.ingestion.web import WebProcessor
from course_supporter.models.source import SourceType
from course_supporter.stt.router import STTRouter


class TestCreateHeavySteps:
    def test_returns_heavy_steps_bundle(self) -> None:
        """Factory returns a HeavySteps with surviving fields populated."""
        heavy = create_heavy_steps()

        assert isinstance(heavy, HeavySteps)
        assert heavy.transcribe is not None
        assert heavy.scrape_web is not None

    def test_takes_no_args(self) -> None:
        """create_heavy_steps takes no args post CA-1 (router param dropped)."""
        heavy = create_heavy_steps()

        assert heavy.transcribe is not None
        assert heavy.scrape_web is not None

    def test_transcribe_is_local_transcribe(self) -> None:
        """transcribe points to local_transcribe."""
        from course_supporter.ingestion.transcribe import local_transcribe

        heavy = create_heavy_steps()
        assert heavy.transcribe is local_transcribe

    def test_scrape_web_is_local_scrape_web(self) -> None:
        """scrape_web points to local_scrape_web."""
        from course_supporter.ingestion.scrape_web import local_scrape_web

        heavy = create_heavy_steps()
        assert heavy.scrape_web is local_scrape_web

    @pytest.mark.parametrize("field", ["transcribe", "scrape_web"])
    def test_heavy_steps_is_frozen(self, field: str) -> None:
        """HeavySteps is immutable — all fields reject assignment."""
        heavy = create_heavy_steps()

        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            setattr(heavy, field, AsyncMock())


class TestCreateProcessors:
    def test_returns_all_source_types(self) -> None:
        """Dict contains all four SourceType keys."""
        heavy = create_heavy_steps()
        mock_router = AsyncMock(spec=STTRouter)
        processors = create_processors(heavy, stt_router=mock_router)

        assert set(processors.keys()) == {
            SourceType.VIDEO,
            SourceType.PRESENTATION,
            SourceType.TEXT,
            SourceType.WEB,
        }

    def test_video_processor_with_stt_router(self) -> None:
        """VIDEO maps to VideoProcessor when stt_router is provided."""
        heavy = create_heavy_steps()
        mock_router = AsyncMock(spec=STTRouter)
        processors = create_processors(heavy, stt_router=mock_router)

        video = processors[SourceType.VIDEO]
        assert isinstance(video, VideoProcessor)
        assert video._stt_router is mock_router

    def test_video_processor_fallback_to_whisper(self) -> None:
        """VIDEO maps to WhisperVideoProcessor when no stt_router."""
        heavy = create_heavy_steps()
        processors = create_processors(heavy)

        video = processors[SourceType.VIDEO]
        assert isinstance(video, WhisperVideoProcessor)
        assert video._transcribe_func is heavy.transcribe

    def test_presentation_processor_type(self) -> None:
        """PRESENTATION maps to PresentationProcessor."""
        heavy = create_heavy_steps()
        processors = create_processors(heavy)

        assert isinstance(processors[SourceType.PRESENTATION], PresentationProcessor)

    def test_text_processor_type(self) -> None:
        """TEXT maps to TextProcessor."""
        heavy = create_heavy_steps()
        processors = create_processors(heavy)

        assert isinstance(processors[SourceType.TEXT], TextProcessor)

    def test_web_processor_type(self) -> None:
        """WEB maps to WebProcessor."""
        heavy = create_heavy_steps()
        processors = create_processors(heavy)

        assert isinstance(processors[SourceType.WEB], WebProcessor)

    # Phase 2.3 sub-area #4: the three tests that asserted on the
    # Design-1 DI attributes (``_parse_pdf_func`` /
    # ``_describe_slides_func``) were removed. The three-stage
    # PresentationProcessor takes no constructor args (Design 2; router
    # arrives via the ``process_macro`` method arg per TextProcessor /
    # WebProcessor precedent), so there is no injected dependency to
    # assert on. ``test_presentation_processor_type`` above is the
    # positive wiring gate.

    def test_web_processor_has_scrape_func(self) -> None:
        """WebProcessor has injected scrape_func."""
        heavy = create_heavy_steps()
        processors = create_processors(heavy)

        web = processors[SourceType.WEB]
        assert isinstance(web, WebProcessor)
        assert web._scrape_func is heavy.scrape_web


class TestCreateProcessorsAudio:
    """KD-2.2-K AUDIO factory dispatch — combined-guard activation."""

    def test_audio_registered_when_both_deps_present(self) -> None:
        """AUDIO entry present + AudioProcessor wired with stt_router + redis."""
        heavy = create_heavy_steps()
        mock_stt = AsyncMock(spec=STTRouter)
        mock_redis = AsyncMock()
        processors = create_processors(heavy, stt_router=mock_stt, redis=mock_redis)

        assert SourceType.AUDIO in processors
        audio = processors[SourceType.AUDIO]
        assert isinstance(audio, AudioProcessor)
        assert audio._stt_router is mock_stt
        assert audio._redis is mock_redis

    def test_audio_absent_when_redis_missing(self) -> None:
        """Combined guard rejects: stt_router present, redis None."""
        heavy = create_heavy_steps()
        mock_stt = AsyncMock(spec=STTRouter)
        processors = create_processors(heavy, stt_router=mock_stt)

        assert SourceType.AUDIO not in processors

    def test_audio_absent_when_stt_router_missing(self) -> None:
        """Combined guard rejects: redis present, stt_router None."""
        heavy = create_heavy_steps()
        mock_redis = AsyncMock()
        processors = create_processors(heavy, redis=mock_redis)

        assert SourceType.AUDIO not in processors
