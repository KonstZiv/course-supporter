"""Factory for heavy step implementations and processor wiring.

Single point of creation for all heavy step callables.
Currently returns local implementations; future: switch to
lambda/serverless implementations via a settings flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from course_supporter.ingestion.audio import AudioProcessor
from course_supporter.ingestion.heavy_steps import (
    ScrapeWebFunc,
    TranscribeFunc,
)
from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.ingestion.text import TextProcessor
from course_supporter.ingestion.video_pipeline import VideoProcessor
from course_supporter.ingestion.web import WebProcessor
from course_supporter.models.source import SourceType

if TYPE_CHECKING:
    from arq.connections import ArqRedis

    from course_supporter.config import Settings
    from course_supporter.ingestion.base import MaterialProcessor
    from course_supporter.llm.router import ModelRouter
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.stt.router import STTRouter
    from course_supporter.vd.pipeline import VDPipeline

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class HeavySteps:
    """Bundle of all heavy step implementations.

    Each field is an async callable conforming to the protocol
    defined in :mod:`course_supporter.ingestion.heavy_steps`.
    """

    transcribe: TranscribeFunc
    scrape_web: ScrapeWebFunc


def create_heavy_steps() -> HeavySteps:
    """Build heavy steps with local implementations.

    Returns:
        HeavySteps bundle with all callable implementations.
    """
    from course_supporter.ingestion.scrape_web import local_scrape_web
    from course_supporter.ingestion.transcribe import local_transcribe

    return HeavySteps(
        transcribe=local_transcribe,
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
        logger.warning("vd_pipeline_skipped_no_router")
        return None

    key_pool = settings.key_pool_for("gemini")
    if key_pool is None:
        logger.warning("vd_pipeline_skipped_no_gemini_keys")
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
    redis: ArqRedis | None = None,
    stage_router: StageRouter | None = None,
) -> dict[SourceType, MaterialProcessor]:
    """Create processor instances wired with heavy steps.

    Args:
        heavy: Bundle of heavy step callables.
        vd_pipeline: Optional VDPipeline (legacy ``vd/`` visual analysis).
            Unused by the new video_pipeline namespace (its Pass 1 wiring
            is ``stage_router`` below). Passing it into the VIDEO processor
            would couple the new namespace to legacy ``vd/`` types
            (isolation per 2.4.1 acceptance #3).
        stt_router: Optional STTRouter for VIDEO + AUDIO transcription.
        redis: Optional ArqRedis client for the STT inter-stage carriers
            (video ``video_stt_result``; audio word-cache KD-2.2-D).
        stage_router: Optional StageRouter for the VIDEO Pass 1 vision
            ladder (Krok 4). VIDEO-only — AUDIO routes its Pass 2a/2c via
            the ``process_macro`` / ``process_detail`` method argument, so
            it needs no injected stage_router.

    Returns:
        Mapping from SourceType to fully-wired processor instances. The
        dispatch guard is **asymmetric**: AUDIO needs STT + Redis; VIDEO
        needs STT + Redis **and** ``stage_router`` (its Pass 1 lives in
        ``process_raw``, before Stage 2 safety, so the vision ladder is
        injected rather than passed to ``process_macro``). Each is absent
        when its deps are unmet; factory dispatch in ``api/tasks.py``
        raises ``KeyError`` for an unwired source type.
    """
    result: dict[SourceType, MaterialProcessor] = {
        SourceType.PRESENTATION: PresentationProcessor(),
        SourceType.TEXT: TextProcessor(),
        SourceType.WEB: WebProcessor(
            scrape_func=heavy.scrape_web,
        ),
    }

    # AUDIO needs STT (stage 1) + a Redis inter-stage carrier. VIDEO needs
    # those two plus the StageRouter for its Krok 4 Pass 1 vision call —
    # injected because Pass 1 runs inside ``process_raw`` (the vision
    # ladder can't arrive via the ``process_macro`` method arg). Phase 2.4
    # task 2.4.2 wired the real VideoProcessor (``ingestion.video_pipeline``);
    # the legacy ``ingestion.video`` processors are removed in 2.4.9.
    if stt_router is not None and redis is not None:
        result[SourceType.AUDIO] = AudioProcessor(
            stt_router=stt_router,
            redis=redis,
        )
        if stage_router is not None:
            result[SourceType.VIDEO] = VideoProcessor(
                stt_router=stt_router,
                redis=redis,
                stage_router=stage_router,
            )

    return result
