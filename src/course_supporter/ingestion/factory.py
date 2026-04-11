"""Factory for heavy step implementations and processor wiring.

Single point of creation for all heavy step callables.
Currently returns local implementations; future: switch to
lambda/serverless implementations via a settings flag.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING

from course_supporter.ingestion.heavy_steps import (
    DescribeSlidesFunc,
    ParsePDFFunc,
    ScrapeWebFunc,
    TranscribeFunc,
)
from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.ingestion.text import TextProcessor
from course_supporter.ingestion.video import VideoProcessor, WhisperVideoProcessor
from course_supporter.ingestion.web import WebProcessor
from course_supporter.models.source import SourceType

if TYPE_CHECKING:
    from course_supporter.config import Settings
    from course_supporter.ingestion.base import SourceProcessor
    from course_supporter.llm.router import ModelRouter
    from course_supporter.stt.router import STTRouter
    from course_supporter.vd.pipeline import VDPipeline


@dataclass(frozen=True, slots=True)
class HeavySteps:
    """Bundle of all heavy step implementations.

    Each field is an async callable conforming to the protocol
    defined in :mod:`course_supporter.ingestion.heavy_steps`.
    ``describe_slides`` is None when no ModelRouter is available;
    PresentationProcessor handles None gracefully (text-only extraction).
    """

    transcribe: TranscribeFunc
    parse_pdf: ParsePDFFunc
    describe_slides: DescribeSlidesFunc | None  # None → text-only PDF
    scrape_web: ScrapeWebFunc


def create_heavy_steps(
    *,
    router: ModelRouter | None = None,
) -> HeavySteps:
    """Build heavy steps with local implementations.

    Args:
        router: ModelRouter instance for Vision LLM calls.
            If provided, ``local_describe_slides`` is bound
            with the router via ``functools.partial``.
            If None, ``describe_slides`` is set to None.

    Returns:
        HeavySteps bundle with all callable implementations.
    """
    from course_supporter.ingestion.parse_pdf import local_parse_pdf
    from course_supporter.ingestion.scrape_web import local_scrape_web
    from course_supporter.ingestion.transcribe import local_transcribe

    describe_slides_func: DescribeSlidesFunc | None = None
    if router is not None:
        from course_supporter.ingestion.describe_slides import (
            local_describe_slides,
        )

        describe_slides_func = functools.partial(local_describe_slides, router=router)

    return HeavySteps(
        transcribe=local_transcribe,
        parse_pdf=local_parse_pdf,
        describe_slides=describe_slides_func,
        scrape_web=local_scrape_web,
    )


def create_vd_pipeline(
    settings: Settings | None = None,
    router: ModelRouter | None = None,
) -> VDPipeline | None:
    """Create VDPipeline from settings, or None if disabled/unconfigured.

    All LLM calls are routed through ``ModelRouter`` for unified cost
    tracking, fallback chains, and tenant attribution.

    Args:
        settings: Application settings. If None, loads from get_settings().
        router: ModelRouter for LLM calls. Required for VD to function.

    Returns:
        Configured VDPipeline, or None if VD is disabled or no Gemini keys.
    """
    if settings is None:
        from course_supporter.config import get_settings

        settings = get_settings()

    if not settings.vd_enabled:
        return None

    if router is None:
        return None

    key_pool = settings.key_pool_for("gemini")
    if key_pool is None:
        return None

    from course_supporter.vd.frame_sampler import FrameSampler
    from course_supporter.vd.memory_pipeline import MemoryPipeline
    from course_supporter.vd.pipeline import VDPipeline
    from course_supporter.vd.rate_limiter import VDRateLimiter
    from course_supporter.vd.visual_analyzer import VisualAnalyzer

    total_rpm = settings.vd_rpm_per_key * len(key_pool)
    rate_limiter = VDRateLimiter(total_rpm)

    memory = MemoryPipeline(router, rate_limiter)
    analyzer = VisualAnalyzer(
        router,
        rate_limiter,
        model=settings.vd_model,
        memory=memory,
    )
    return VDPipeline(
        sampler=FrameSampler(),
        analyzer=analyzer,
        memory=memory,
    )


def create_processors(
    heavy: HeavySteps,
    *,
    vd_pipeline: VDPipeline | None = None,
    stt_router: STTRouter | None = None,
) -> dict[SourceType, SourceProcessor]:
    """Create processor instances wired with heavy steps.

    Args:
        heavy: Bundle of heavy step callables.
        vd_pipeline: Optional VDPipeline for video visual analysis.
        stt_router: Optional STTRouter for video transcription.
            When provided, VideoProcessor uses STTRouter instead of Whisper.
            When None, falls back to WhisperVideoProcessor.

    Returns:
        Mapping from SourceType to fully-wired processor instances.
    """
    video_processor: SourceProcessor
    if stt_router is not None:
        video_processor = VideoProcessor(
            stt_router=stt_router,
            vd_pipeline=vd_pipeline,
        )
    else:
        video_processor = WhisperVideoProcessor(
            transcribe_func=heavy.transcribe,
        )

    return {
        SourceType.VIDEO: video_processor,
        SourceType.PRESENTATION: PresentationProcessor(
            parse_pdf_func=heavy.parse_pdf,
            describe_slides_func=heavy.describe_slides,
        ),
        SourceType.TEXT: TextProcessor(),
        SourceType.WEB: WebProcessor(
            scrape_func=heavy.scrape_web,
        ),
    }
