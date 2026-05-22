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
) -> dict[SourceType, MaterialProcessor]:
    """Create processor instances wired with heavy steps.

    Args:
        heavy: Bundle of heavy step callables.
        vd_pipeline: Optional VDPipeline (legacy ``vd/`` visual analysis).
            Unused since Phase 2.4 task 2.4.1 — the new skeleton
            VideoProcessor takes no deps; retained for the real Pass 1
            vision wiring (task 2.4.4).
        stt_router: Optional STTRouter for audio transcription (and, from
            task 2.4.2, video STT). AudioProcessor uses it today; the
            VIDEO skeleton does not yet consume it.
        redis: Optional ArqRedis client for the audio word-cache
            (KD-2.2-D). AudioProcessor is registered only when both
            ``stt_router`` and ``redis`` are non-None — both dependencies
            are required for the three-stage pipeline (STT in stage 1 +
            cache hand-off across stages).

    Returns:
        Mapping from SourceType to fully-wired processor instances.
        ``SourceType.AUDIO`` is absent from the result when the combined
        guard above is unsatisfied; factory dispatch in ``api/tasks.py``
        raises ``KeyError`` for unwired source types.
    """
    # Phase 2.4 task 2.4.1 — VIDEO routes to the new skeleton
    # VideoProcessor (``ingestion.video_pipeline``). Its 7-step pipeline is
    # stubbed (zero external calls); real edges land in tasks 2.4.2-2.4.7
    # and the legacy ``ingestion.video`` processors are removed in 2.4.9.
    # The skeleton takes no constructor deps — ``stt_router`` (real STT,
    # task 2.4.2) and ``vd_pipeline`` (real Pass 1 vision, task 2.4.4) are
    # retained on the factory for that future wiring; passing them into
    # the skeleton would couple the new namespace to legacy ``vd/`` types
    # (isolation per 2.4.1 acceptance #3).
    result: dict[SourceType, MaterialProcessor] = {
        SourceType.VIDEO: VideoProcessor(),
        SourceType.PRESENTATION: PresentationProcessor(),
        SourceType.TEXT: TextProcessor(),
        SourceType.WEB: WebProcessor(
            scrape_func=heavy.scrape_web,
        ),
    }

    if stt_router is not None and redis is not None:
        result[SourceType.AUDIO] = AudioProcessor(
            stt_router=stt_router,
            redis=redis,
        )

    return result
