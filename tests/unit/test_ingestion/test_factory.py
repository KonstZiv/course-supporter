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
from course_supporter.ingestion.video_pipeline import VideoProcessor
from course_supporter.ingestion.web import WebProcessor
from course_supporter.llm.stage_router import StageRouter
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
        """All five keys present with STT + Redis + StageRouter (VIDEO needs all)."""
        heavy = create_heavy_steps()
        mock_router = AsyncMock(spec=STTRouter)
        mock_redis = AsyncMock()
        processors = create_processors(
            heavy,
            stt_router=mock_router,
            redis=mock_redis,
            stage_router=AsyncMock(spec=StageRouter),
        )

        assert set(processors.keys()) == {
            SourceType.VIDEO,
            SourceType.PRESENTATION,
            SourceType.TEXT,
            SourceType.WEB,
            SourceType.AUDIO,
        }

    def test_video_maps_to_video_pipeline_processor(self) -> None:
        """VIDEO maps to the ``ingestion.video_pipeline`` VideoProcessor.

        Task 2.4.4 added a third VIDEO dep — ``stage_router`` for the Krok 4
        Pass 1 vision ladder. The dispatch guard is asymmetric: VIDEO needs
        STT + Redis **and** stage_router; AUDIO needs only STT + Redis (its
        Pass 2a/2c route via the method argument). VIDEO is absent if any of
        its three deps is missing.
        """
        heavy = create_heavy_steps()
        mock_router = AsyncMock(spec=STTRouter)
        mock_redis = AsyncMock()
        mock_stage = AsyncMock(spec=StageRouter)

        with_deps = create_processors(
            heavy,
            stt_router=mock_router,
            redis=mock_redis,
            stage_router=mock_stage,
        )
        video = with_deps[SourceType.VIDEO]
        assert isinstance(video, VideoProcessor)
        # All three deps are actually wired in.
        assert video._stt_router is mock_router
        assert video._redis is mock_redis
        assert video._stage_router is mock_stage

        without_redis = create_processors(
            heavy, stt_router=mock_router, stage_router=mock_stage
        )
        assert SourceType.VIDEO not in without_redis

        # D2 split: without stage_router, VIDEO is absent but AUDIO remains.
        without_stage = create_processors(
            heavy, stt_router=mock_router, redis=mock_redis
        )
        assert SourceType.VIDEO not in without_stage
        assert SourceType.AUDIO in without_stage

        without_deps = create_processors(heavy)
        assert SourceType.VIDEO not in without_deps

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
