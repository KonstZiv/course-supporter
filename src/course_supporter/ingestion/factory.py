"""Factory for heavy step implementations and processor wiring.

Single point of creation for all heavy step callables.
Currently returns local implementations; future: switch to
lambda/serverless implementations via a settings flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from course_supporter.ingestion.audio import AudioProcessor
from course_supporter.ingestion.code import CodeProcessor
from course_supporter.ingestion.heavy_steps import ScrapeWebFunc
from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.ingestion.text import TextProcessor
from course_supporter.ingestion.video_pipeline import VideoProcessor
from course_supporter.ingestion.web import WebProcessor
from course_supporter.models.source import SourceType

if TYPE_CHECKING:
    from arq.connections import ArqRedis

    from course_supporter.ingestion.base import MaterialProcessor
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.stt.router import STTRouter


@dataclass(frozen=True, slots=True)
class HeavySteps:
    """Bundle of all heavy step implementations.

    Each field is an async callable conforming to the protocol
    defined in :mod:`course_supporter.ingestion.heavy_steps`.
    """

    scrape_web: ScrapeWebFunc


def create_heavy_steps() -> HeavySteps:
    """Build heavy steps with local implementations.

    Returns:
        HeavySteps bundle with all callable implementations.
    """
    from course_supporter.ingestion.scrape_web import local_scrape_web

    return HeavySteps(
        scrape_web=local_scrape_web,
    )


def create_processors(
    heavy: HeavySteps,
    *,
    stt_router: STTRouter | None = None,
    redis: ArqRedis | None = None,
    stage_router: StageRouter | None = None,
) -> dict[SourceType, MaterialProcessor]:
    """Create processor instances wired with heavy steps.

    Args:
        heavy: Bundle of heavy step callables.
        stt_router: Optional STTRouter for VIDEO + AUDIO transcription.
        redis: Optional ArqRedis client for the STT inter-stage carriers
            (video ``video_stt_result``; audio word-cache KD-2.2-D).
        stage_router: StageRouter injected into both audio + video
            processors. VIDEO needs it for the Krok 4 Pass 1 vision ladder
            (inside ``process_raw``, before Stage 2 safety). AUDIO needs it
            for Pass 2c selective denoise: the orchestrator passes a router
            only to ``process_macro``, never to ``process_detail``, so audio
            Pass 2c (task 2.4.7) sources the injected router — without it the
            denoise stays inert (the latent gap closed here). Optional so an
            unmet dep simply leaves Pass 2c skipping; in production
            ``api/tasks.py`` always supplies it from the job ctx.

    Returns:
        Mapping from SourceType to fully-wired processor instances. The
        dispatch guard is **asymmetric**: AUDIO needs STT + Redis (and takes
        ``stage_router`` for Pass 2c when present); VIDEO needs STT + Redis
        **and** ``stage_router`` (its Pass 1 lives in ``process_raw``, before
        Stage 2 safety, so the vision ladder is injected rather than passed
        to ``process_macro``). Each is absent when its required deps are
        unmet; factory dispatch in ``api/tasks.py`` raises ``KeyError`` for
        an unwired source type.
    """
    result: dict[SourceType, MaterialProcessor] = {
        SourceType.PRESENTATION: PresentationProcessor(),
        SourceType.TEXT: TextProcessor(),
        SourceType.WEB: WebProcessor(
            scrape_func=heavy.scrape_web,
        ),
        # task-code-materials: unconditional like TEXT/WEB — the LLM
        # stages arrive via the process_macro router argument, no
        # constructor deps.
        SourceType.CODE: CodeProcessor(),
    }

    # AUDIO needs STT (stage 1) + a Redis inter-stage carrier; it also takes
    # the StageRouter for Pass 2c selective denoise (task 2.4.7 — the
    # orchestrator never passes a router to process_detail, so the injected
    # one activates the denoise). VIDEO needs STT + Redis plus the StageRouter
    # for its Krok 4 Pass 1 vision call — injected because Pass 1 runs inside
    # ``process_raw`` (the vision ladder can't arrive via the ``process_macro``
    # method arg). The new ``ingestion.video_pipeline`` VideoProcessor is the
    # only video implementation (the legacy ``ingestion.video`` + ``vd/`` stack
    # was removed in task 2.4.9A).
    if stt_router is not None and redis is not None:
        result[SourceType.AUDIO] = AudioProcessor(
            stt_router=stt_router,
            redis=redis,
            stage_router=stage_router,
        )
        if stage_router is not None:
            result[SourceType.VIDEO] = VideoProcessor(
                stt_router=stt_router,
                redis=redis,
                stage_router=stage_router,
            )

    return result
