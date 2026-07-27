"""MaterialProcessor abstract base class and custom exceptions.

Defines the three-stage ingestion protocol used by all source-type
processors (video, audio, presentation, text, web). Stage 1
(``process_raw``) is implemented by every subclass today; Stages 5/6
(``process_macro``, ``process_detail``) are stubbed in the base with
``NotImplementedError`` and will be filled in per-source in follow-up
PRs of the Content Ingestion roadmap.

Phase 2.1 commit 4.5 consolidated the Stage 5/6 signatures per
KD-2.1-M:

* ``process_macro(doc, router)`` returns a
  :class:`~course_supporter.ingestion.schemas.DocumentSummaryDraft`
  (a Pydantic carrier, not an ORM row) and requires a
  :class:`~course_supporter.llm.stage_router.StageRouter`.
* ``process_detail(doc, summary_draft)`` returns
  ``list[DocumentSegmentDraft]`` and accepts the upstream summary
  draft from Pass 2a directly (no router argument -- Pass 2b is
  algorithmic for text/web; future LLM-cleaning impls will wire
  their own router from the processor instance).

Concrete overrides (TextProcessor / WebProcessor) arrive in commits
5 and 7.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.models.source import SourceDocument

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.security.exceptions import ErrorCategory
    from course_supporter.storage.orm import AuthoredDocument


class ProcessingError(Exception):
    """Raised when a processor fails to process source material."""


class CategorisedProcessingError(ProcessingError):
    """ProcessingError carrying a structural async-error code (F4).

    task-code-materials / DD-2.3-AH: the ``category`` (a security
    :class:`~course_supporter.security.exceptions.ErrorCategory`) rides
    the failure path into ``Job.error_category`` +
    ``AuthoredDocument.error_category`` so the author UI resolves a
    stable message instead of parsing free-text ``error_message``.
    """

    def __init__(self, category: ErrorCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


class UnsupportedFormatError(ProcessingError):
    """Raised when source material format is not supported by processor."""


class MaterialProcessor(abc.ABC):
    """Abstract base class for all source material processors.

    Encodes the three-stage Content Ingestion pipeline:

    1. :meth:`process_raw` -- parse raw upload into a ``SourceDocument``
       (Stage 1). Implemented by every subclass today.
    2. :meth:`process_macro` -- generate a single
       :class:`DocumentSummaryDraft` for the document (Pass 2a; LLM
       call routed through :class:`StageRouter` per KD16).
    3. :meth:`process_detail` -- produce
       ``list[DocumentSegmentDraft]`` for a given summary draft
       (Pass 2b; algorithmic for text/web, LLM for media in later
       phases).

    Stages 5 and 6 currently stub to ``NotImplementedError`` in the
    base class and are filled in per-source by PRs from Phase 2.1
    commits 5 (Pass 2a) and 7 (Pass 2b).
    """

    @abc.abstractmethod
    async def process_raw(
        self,
        source: AuthoredDocument,
    ) -> SourceDocument:
        """Parse raw source material into a structured ``SourceDocument``.

        Args:
            source: The source material to process.

        Returns:
            SourceDocument with extracted content chunks.

        Raises:
            ProcessingError: If processing fails.
            UnsupportedFormatError: If source format is not supported.
        """
        ...

    async def process_macro(
        self,
        doc: SourceDocument,
        router: StageRouter,
    ) -> DocumentSummaryDraft:
        """Generate the table-of-contents summary draft for ``doc``.

        Default implementation raises ``NotImplementedError``. Each
        processor overrides this once its Pass 2a pipeline lands
        (Phase 2.1 commit 5 for text/web).
        """
        raise NotImplementedError(
            f"{type(self).__name__}.process_macro is not yet implemented"
        )

    async def process_detail(
        self,
        doc: SourceDocument,
        summary_draft: DocumentSummaryDraft,
    ) -> list[DocumentSegmentDraft]:
        """Produce the segment drafts for ``doc`` under ``summary_draft``.

        Default implementation raises ``NotImplementedError``. Each
        processor overrides this once its Pass 2b pipeline lands
        (Phase 2.1 commit 7 for text/web).
        """
        raise NotImplementedError(
            f"{type(self).__name__}.process_detail is not yet implemented"
        )
