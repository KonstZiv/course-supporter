"""Tests for WebProcessor."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.heavy_steps import ScrapedContent
from course_supporter.ingestion.web import WebProcessor
from course_supporter.models.source import ChunkType, SourceDocument, SourceType


def _make_source(
    source_type: str = "web",
    url: str = "https://example.com/article",
    filename: str | None = None,
) -> MagicMock:
    """Create a mock SourceMaterial."""
    source = MagicMock()
    source.source_type = source_type
    source.source_url = url
    source.filename = filename
    return source


def _mock_scrape_func(
    text: str = "Extracted paragraph one\n\nParagraph two",
    raw_html: str = "<html>content</html>",
) -> AsyncMock:
    """Create a mock ScrapeWebFunc returning ScrapedContent."""
    return AsyncMock(return_value=ScrapedContent(text=text, raw_html=raw_html))


class TestWebProcessor:
    async def test_fetch_success(self) -> None:
        """Injected scrape_func -> SourceDocument with WEB_CONTENT chunks."""
        proc = WebProcessor(scrape_func=_mock_scrape_func())
        doc = await proc.process(_make_source())

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.WEB
        assert len(doc.chunks) == 2
        assert doc.chunks[0].chunk_type == ChunkType.WEB_CONTENT

    async def test_fetch_failure(self) -> None:
        """scrape_func raises ProcessingError -> propagated."""
        mock_func = AsyncMock(
            side_effect=ProcessingError("Failed to fetch URL (no response): url")
        )
        proc = WebProcessor(scrape_func=mock_func)
        with pytest.raises(ProcessingError, match="Failed to fetch URL"):
            await proc.process(_make_source())

    async def test_extract_empty(self) -> None:
        """Empty text from scrape_func -> empty chunks (not an error)."""
        mock = _mock_scrape_func(text="", raw_html="<html></html>")
        proc = WebProcessor(scrape_func=mock)
        doc = await proc.process(_make_source())

        assert doc.chunks == []

    async def test_domain_in_metadata(self) -> None:
        """URL domain extracted to metadata."""
        proc = WebProcessor(scrape_func=_mock_scrape_func(text="text"))
        doc = await proc.process(
            _make_source(url="https://docs.python.org/3/tutorial.html")
        )

        assert doc.metadata["domain"] == "docs.python.org"

    async def test_invalid_source_type(self) -> None:
        """Non-web source_type -> UnsupportedFormatError."""
        proc = WebProcessor(scrape_func=_mock_scrape_func())
        with pytest.raises(UnsupportedFormatError, match="expects 'web'"):
            await proc.process(_make_source(source_type="text"))

    async def test_content_snapshot(self) -> None:
        """Raw HTML saved in metadata for later re-processing."""
        raw_html = "<html><body>Raw content</body></html>"
        proc = WebProcessor(scrape_func=_mock_scrape_func(raw_html=raw_html))
        doc = await proc.process(_make_source())

        assert doc.metadata["content_snapshot"] == raw_html

    async def test_chunks_indexed(self) -> None:
        """Multiple paragraphs -> ordered chunks."""
        proc = WebProcessor(
            scrape_func=_mock_scrape_func(text="Para 1\n\nPara 2\n\nPara 3")
        )
        doc = await proc.process(_make_source())

        assert len(doc.chunks) == 3
        indices = [c.index for c in doc.chunks]
        assert indices == [0, 1, 2]

    async def test_fetched_at_in_metadata(self) -> None:
        """fetched_at timestamp present in metadata."""
        proc = WebProcessor(scrape_func=_mock_scrape_func())
        doc = await proc.process(_make_source())

        assert "fetched_at" in doc.metadata

    async def test_scrape_func_called_with_params(self) -> None:
        """scrape_func receives URL and ScrapeWebParams."""
        mock_func = _mock_scrape_func()
        proc = WebProcessor(scrape_func=mock_func)
        await proc.process(_make_source(url="https://example.com/page"))

        mock_func.assert_awaited_once()
        args = mock_func.call_args
        assert args[0][0] == "https://example.com/page"


class TestWebProcessorDefaults:
    def test_default_scrape_func_is_local_scrape_web(self) -> None:
        """WebProcessor() without args uses local_scrape_web."""
        from course_supporter.ingestion.scrape_web import local_scrape_web

        proc = WebProcessor()
        assert proc._scrape_func is local_scrape_web

    def test_injected_func_used_instead_of_default(self) -> None:
        """Explicit scrape_func overrides the default."""
        custom = AsyncMock()
        proc = WebProcessor(scrape_func=custom)
        assert proc._scrape_func is custom
