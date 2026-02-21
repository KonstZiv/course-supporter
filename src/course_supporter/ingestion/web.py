"""Web processor — thin orchestrator over ScrapeWebFunc."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog

from course_supporter.ingestion.base import (
    SourceProcessor,
    UnsupportedFormatError,
)
from course_supporter.ingestion.heavy_steps import (
    ScrapeWebFunc,
    ScrapeWebParams,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.storage.orm import SourceMaterial

logger = structlog.get_logger()


class WebProcessor(SourceProcessor):
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

    async def process(
        self,
        source: SourceMaterial,
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
