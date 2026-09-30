"""Web processor — thin orchestrator over ScrapeWebFunc."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog

from course_supporter.ingestion.base import (
    MaterialProcessor,
    UnsupportedFormatError,
)
from course_supporter.ingestion.heavy_steps import (
    ScrapeWebFunc,
    ScrapeWebParams,
)
from course_supporter.ingestion.paragraph_anchors import compute_paragraph_anchors
from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.ingestion.text_mapping import map_text_document
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.storage.orm import AuthoredDocument

logger = structlog.get_logger()

# Web chunks are all paragraphs (WEB_CONTENT); no headings to skip.
_WEB_PARAGRAPH_TYPES = frozenset({ChunkType.WEB_CONTENT})


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
        """Pass 2a -- LLM maps the document into segments (KD-2.1-A).

        Shared by TextProcessor and WebProcessor: see
        :func:`~course_supporter.ingestion.text_mapping.map_text_document`
        (numbered-line prompt, line ranges converted to char offsets
        server-side, concepts aggregated over segments).
        """
        return await map_text_document(doc, router)

    async def process_detail(
        self,
        doc: SourceDocument,
        summary_draft: DocumentSummaryDraft,
    ) -> list[DocumentSegmentDraft]:
        """Pass 2b -- algorithmic slice over Pass 2a offsets (KD-2.1-O).

        Zero LLM calls. Mirrors ``TextProcessor.process_detail``: slices
        ``doc.assemble_text()`` per draft offset pair. Reference text is
        the string Pass 2a numbered for the mapping prompt and converted
        the model's line ranges over. Non-None
        ``draft.content`` is passed through verbatim (defensive).
        """
        reference_text = doc.assemble_text()
        # Paragraph anchors (Phase 3.3a): every WEB_CONTENT chunk is a
        # paragraph (no headings to skip), so each emitted chunk advances
        # the ordinal.
        emitted = [c for c in doc.chunks if c.text]
        filled: list[DocumentSegmentDraft] = []
        for draft in summary_draft.segments:
            start_para, end_para = compute_paragraph_anchors(
                emitted, _WEB_PARAGRAPH_TYPES, draft.start_pos, draft.end_pos
            )
            update: dict[str, object] = {
                "start_paragraph": start_para,
                "end_paragraph": end_para,
            }
            if draft.content is None:
                update["content"] = reference_text[draft.start_pos : draft.end_pos]
            filled.append(draft.model_copy(update=update))
        return filled
