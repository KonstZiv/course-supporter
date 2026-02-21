"""Tests for heavy steps factory."""

from __future__ import annotations

import functools
from unittest.mock import AsyncMock

import pytest

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


class TestCreateHeavySteps:
    def test_with_router(self) -> None:
        """All three fields populated when router is provided."""
        router = AsyncMock()
        heavy = create_heavy_steps(router=router)

        assert isinstance(heavy, HeavySteps)
        assert heavy.transcribe is not None
        assert heavy.describe_slides is not None
        assert heavy.scrape_web is not None

    def test_without_router(self) -> None:
        """describe_slides is None when no router provided."""
        heavy = create_heavy_steps()

        assert heavy.transcribe is not None
        assert heavy.describe_slides is None
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

    def test_describe_slides_bound_with_router(self) -> None:
        """describe_slides is a partial with router already bound."""
        router = AsyncMock()
        heavy = create_heavy_steps(router=router)

        assert heavy.describe_slides is not None
        assert isinstance(heavy.describe_slides, functools.partial)
        assert heavy.describe_slides.keywords["router"] is router

    def test_describe_slides_wraps_local_describe_slides(self) -> None:
        """The partial wraps local_describe_slides."""
        from course_supporter.ingestion.describe_slides import (
            local_describe_slides,
        )

        router = AsyncMock()
        heavy = create_heavy_steps(router=router)

        assert isinstance(heavy.describe_slides, functools.partial)
        assert heavy.describe_slides.func is local_describe_slides

    @pytest.mark.parametrize("field", ["transcribe", "describe_slides", "scrape_web"])
    def test_heavy_steps_is_frozen(self, field: str) -> None:
        """HeavySteps is immutable — all fields reject assignment."""
        router = AsyncMock()
        heavy = create_heavy_steps(router=router)

        with pytest.raises(AttributeError):
            setattr(heavy, field, AsyncMock())


class TestCreateProcessors:
    def test_returns_all_source_types(self) -> None:
        """Dict contains all four SourceType keys."""
        heavy = create_heavy_steps()
        processors = create_processors(heavy)

        assert set(processors.keys()) == {
            SourceType.VIDEO,
            SourceType.PRESENTATION,
            SourceType.TEXT,
            SourceType.WEB,
        }

    def test_video_processor_type(self) -> None:
        """VIDEO maps to VideoProcessor."""
        heavy = create_heavy_steps()
        processors = create_processors(heavy)

        assert isinstance(processors[SourceType.VIDEO], VideoProcessor)

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

    def test_video_processor_has_transcribe_func(self) -> None:
        """VideoProcessor's WhisperVideoProcessor has injected transcribe."""
        heavy = create_heavy_steps()
        processors = create_processors(heavy)

        video = processors[SourceType.VIDEO]
        assert isinstance(video, VideoProcessor)
        assert isinstance(video._whisper, WhisperVideoProcessor)
        assert video._whisper._transcribe_func is heavy.transcribe

    def test_presentation_processor_has_describe_func(self) -> None:
        """PresentationProcessor has injected describe_slides_func."""
        router = AsyncMock()
        heavy = create_heavy_steps(router=router)
        processors = create_processors(heavy)

        pres = processors[SourceType.PRESENTATION]
        assert isinstance(pres, PresentationProcessor)
        assert pres._describe_slides_func is heavy.describe_slides

    def test_presentation_processor_none_without_router(self) -> None:
        """PresentationProcessor has None describe_slides when no router."""
        heavy = create_heavy_steps()
        processors = create_processors(heavy)

        pres = processors[SourceType.PRESENTATION]
        assert isinstance(pres, PresentationProcessor)
        assert pres._describe_slides_func is None

    def test_web_processor_has_scrape_func(self) -> None:
        """WebProcessor has injected scrape_func."""
        heavy = create_heavy_steps()
        processors = create_processors(heavy)

        web = processors[SourceType.WEB]
        assert isinstance(web, WebProcessor)
        assert web._scrape_func is heavy.scrape_web
