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
    ScrapeWebFunc,
    TranscribeFunc,
)
from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.ingestion.text import TextProcessor
from course_supporter.ingestion.video import VideoProcessor
from course_supporter.ingestion.web import WebProcessor
from course_supporter.models.source import SourceType

if TYPE_CHECKING:
    from course_supporter.ingestion.base import SourceProcessor
    from course_supporter.llm.router import ModelRouter


@dataclass(frozen=True, slots=True)
class HeavySteps:
    """Bundle of all heavy step implementations.

    Each field is an async callable conforming to the protocol
    defined in :mod:`course_supporter.ingestion.heavy_steps`.
    ``describe_slides`` is None when no ModelRouter is available;
    PresentationProcessor handles None gracefully (text-only extraction).
    """

    transcribe: TranscribeFunc
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
        describe_slides=describe_slides_func,
        scrape_web=local_scrape_web,
    )


def create_processors(
    heavy: HeavySteps,
) -> dict[SourceType, SourceProcessor]:
    """Create processor instances wired with heavy steps.

    Args:
        heavy: Bundle of heavy step callables.

    Returns:
        Mapping from SourceType to fully-wired processor instances.
    """
    return {
        SourceType.VIDEO: VideoProcessor(
            transcribe_func=heavy.transcribe,
        ),
        SourceType.PRESENTATION: PresentationProcessor(
            describe_slides_func=heavy.describe_slides,
        ),
        SourceType.TEXT: TextProcessor(),
        SourceType.WEB: WebProcessor(
            scrape_func=heavy.scrape_web,
        ),
    }
