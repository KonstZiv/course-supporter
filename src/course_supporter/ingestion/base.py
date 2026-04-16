"""MaterialProcessor abstract base class and custom exceptions.

Defines the three-stage ingestion protocol used by all source-type
processors (video, audio, presentation, text, web). Stage 1
(``process_raw``) is implemented by every subclass today; Stages 5/6
(``process_macro``, ``process_detail``) are stubbed in the base with
``NotImplementedError`` and will be filled in per-source in follow-up
PRs of the Content Ingestion roadmap.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from course_supporter.models.source import SourceDocument

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.storage.orm import (
        MaterialEntry,
        MaterialMacroSection,
        MaterialSegment,
    )


class ProcessingError(Exception):
    """Raised when a processor fails to process source material."""


class UnsupportedFormatError(ProcessingError):
    """Raised when source material format is not supported by processor."""


class MaterialProcessor(abc.ABC):
    """Abstract base class for all source material processors.

    Encodes the three-stage Content Ingestion pipeline:

    1. :meth:`process_raw` — parse raw upload into a ``SourceDocument``
       (Stage 1). Implemented by every subclass today.
    2. :meth:`process_macro` — generate table-of-contents sections
       (Stage 5, LLM for video/audio/presentation, ladder for text/web).
    3. :meth:`process_detail` — produce cleaned or sliced segments
       per macro section (Stage 6).

    Stages 5 and 6 currently stub to ``NotImplementedError`` in the
    base class and are filled in per-source by PRs #4c+.
    """

    @abc.abstractmethod
    async def process_raw(
        self,
        source: MaterialEntry,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        """Parse raw source material into a structured ``SourceDocument``.

        Args:
            source: The source material to process.
            router: Optional ModelRouter for LLM-powered processing
                    (vision analysis, transcription via Gemini, etc.).

        Returns:
            SourceDocument with extracted content chunks.

        Raises:
            ProcessingError: If processing fails.
            UnsupportedFormatError: If source format is not supported.
        """
        ...

    async def process_macro(
        self,
        source: MaterialEntry,
        *,
        router: ModelRouter | None = None,
    ) -> list[MaterialMacroSection]:
        """Generate table-of-contents sections for the given material.

        Default implementation raises ``NotImplementedError``. Each
        processor will override this once the Stage 5 pipeline lands
        (PR #4c+).
        """
        raise NotImplementedError(
            f"{type(self).__name__}.process_macro is not yet implemented"
        )

    async def process_detail(
        self,
        macro: MaterialMacroSection,
        *,
        router: ModelRouter | None = None,
    ) -> list[MaterialSegment]:
        """Produce cleaned/sliced segments for a single macro section.

        Default implementation raises ``NotImplementedError``. Each
        processor will override this once the Stage 6 pipeline lands
        (PR #4c+).
        """
        raise NotImplementedError(
            f"{type(self).__name__}.process_detail is not yet implemented"
        )
