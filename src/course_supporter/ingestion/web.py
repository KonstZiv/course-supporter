"""Web processor using trafilatura for content extraction."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog

from course_supporter.ingestion.base import (
    ProcessingError,
    SourceProcessor,
    UnsupportedFormatError,
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
    """Process web pages by fetching HTML and extracting content.

    Uses trafilatura for intelligent content extraction
    (article text, removing boilerplate/navigation).
    Raw HTML is saved as content_snapshot for re-processing.
    """

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

        # 1. Fetch HTML
        html = self._fetch_html(url)

        # 2. Extract content
        extracted = self._extract_content(html)

        # 3. Split into chunks
        chunks = self._text_to_chunks(extracted) if extracted else []

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
                "content_snapshot": html,
            },
        )

    @staticmethod
    def _fetch_html(url: str) -> str:
        """Fetch HTML from URL using trafilatura.

        Args:
            url: The URL to fetch.

        Returns:
            Raw HTML string.

        Raises:
            ProcessingError: If fetch fails (returns None).
        """
        import trafilatura

        result = trafilatura.fetch_url(url)
        if result is None:
            raise ProcessingError(
                f"Failed to fetch URL: {url}. The page may be unreachable or blocked."
            )
        return str(result)

    @staticmethod
    def _extract_content(html: str) -> str | None:
        """Extract main content from HTML using trafilatura.

        Args:
            html: Raw HTML string.

        Returns:
            Extracted text content, or None if extraction fails.
        """
        import trafilatura

        result = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
        )
        if result is None:
            return None
        return str(result)

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
