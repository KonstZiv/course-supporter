"""Web processor — thin orchestrator over ScrapeWebFunc."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog

from course_supporter.ingestion.base import (
    MaterialProcessor,
    ProcessingError,
    UnsupportedFormatError,
)
from course_supporter.ingestion.heavy_steps import (
    ScrapeWebFunc,
    ScrapeWebParams,
)
from course_supporter.ingestion.schemas import DocumentSummaryDraft
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.storage.orm import AuthoredDocument

logger = structlog.get_logger()


class WebProcessor(MaterialProcessor):
    """Process web pages by delegating to an injected scraping function.

    Uses ``ScrapeWebFunc`` for content extraction (default: ``local_scrape_web``).
    Raw HTML is saved as content_snapshot for re-processing.
    """

    def __init__(
        self,
        *,
        scrape_func: ScrapeWebFunc | None = None,
    ) -> None:
        self._scrape_func = scrape_func or self._default_scrape_func()

    @staticmethod
    def _default_scrape_func() -> ScrapeWebFunc:
        """Lazy-import local_scrape_web as the default implementation."""
        from course_supporter.ingestion.scrape_web import local_scrape_web

        return local_scrape_web

    async def process_raw(
        self,
        source: AuthoredDocument,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        if source.source_type != SourceType.WEB:
            raise UnsupportedFormatError(
                f"WebProcessor expects 'web', got '{source.source_type}'"
            )

        url = source.source_url
        parsed_url = urlparse(url)
        domain = parsed_url.netloc

        logger.info("web_processing_start", url=url, domain=domain)

        # 1. Delegate to heavy step
        scraped = await self._scrape_func(url, ScrapeWebParams())

        # 2. Split into chunks
        chunks = self._text_to_chunks(scraped.text) if scraped.text else []

        fetched_at = datetime.now(UTC).isoformat()

        logger.info(
            "web_processing_done",
            url=url,
            chunk_count=len(chunks),
        )

        return SourceDocument(
            source_type=SourceType.WEB,
            source_url=url,
            title=source.filename or domain,
            chunks=chunks,
            metadata={
                "domain": domain,
                "fetched_at": fetched_at,
                "content_snapshot": scraped.raw_html,
            },
        )

    @staticmethod
    def _text_to_chunks(text: str) -> list[ContentChunk]:
        """Split extracted text into content chunks.

        Splits on double newlines to create paragraph-like chunks.
        """
        chunks: list[ContentChunk] = []
        paragraphs = text.strip().split("\n\n")

        for idx, para in enumerate(paragraphs):
            para = para.strip()
            if not para:
                continue
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.WEB_CONTENT,
                    text=para,
                    index=idx,
                )
            )

        return chunks

    async def process_macro(
        self,
        doc: SourceDocument,
        router: StageRouter,
    ) -> DocumentSummaryDraft:
        """Pass 2a -- premium LLM extracts concept structure (KD-2.1-A).

        Concatenates chunk text into a single body and routes the
        resulting document through the ``pass_2a_mapping`` stage.
        Returns a :class:`DocumentSummaryDraft`; segment drafts are
        retained on the result for Phase 2.1 C7 (Pass 2b) consumption
        and are not materialised here.

        Document-level ``main_concepts`` / ``secondary_concepts`` are
        derived algorithmically as ``union + dedup`` over the per-segment
        concepts (vision.md §2.2, KD-2.1-O). The LLM is asked to emit
        concepts only at segment level — this method assembles the
        document-level view by sorted set-union and applies the
        conflict rule: any concept that appears as ``main`` in at
        least one segment stays in ``main_concepts`` and is removed
        from ``secondary_concepts``.
        """
        text = "\n\n".join(chunk.text for chunk in doc.chunks if chunk.text)
        if not text.strip():
            msg = "Cannot run Pass 2a on empty document (no content chunks)"
            raise ProcessingError(msg)
        result = await router.execute_for_stage(
            "pass_2a_mapping",
            text=text,
        )
        draft = DocumentSummaryDraft.model_validate_json(result.content)

        # Algorithmic aggregation of document-level concepts from
        # per-segment concepts (vision.md §2.2, KD-2.1-O). LLM emits
        # concepts only at segment level; ``DocumentSummary.main_concepts``
        # is the sorted set-union over all segments. Conflict rule:
        # any concept that appears as ``main`` in at least one segment
        # stays in ``main_concepts`` and is removed from
        # ``secondary_concepts``.
        all_main: set[str] = set()
        all_secondary: set[str] = set()
        for seg in draft.segments:
            all_main.update(seg.main_concepts)
            all_secondary.update(seg.secondary_concepts)
        all_secondary -= all_main
        draft.main_concepts = sorted(all_main)
        draft.secondary_concepts = sorted(all_secondary)

        return draft
