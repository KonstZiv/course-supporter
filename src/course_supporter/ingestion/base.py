"""SourceProcessor abstract base class and custom exceptions."""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from course_supporter.models.source import SourceDocument

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.storage.orm import SourceMaterial


class ProcessingError(Exception):
    """Raised when a processor fails to process source material."""


class UnsupportedFormatError(ProcessingError):
    """Raised when source material format is not supported by processor."""


class SourceProcessor(abc.ABC):
    """Abstract base class for all source material processors.

    Each processor transforms a SourceMaterial into a SourceDocument
    containing extracted ContentChunks.
    """

    @abc.abstractmethod
    async def process(
        self,
        source: SourceMaterial,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        """Process source material and return structured document.

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
