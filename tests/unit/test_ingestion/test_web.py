"""Tests for WebProcessor."""

from unittest.mock import MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
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


class TestWebProcessor:
    async def test_fetch_success(self) -> None:
        """Mock trafilatura -> SourceDocument with WEB_CONTENT chunks."""
        with (
            patch(
                "trafilatura.fetch_url",
                return_value="<html>content</html>",
            ),
            patch(
                "trafilatura.extract",
                return_value="Extracted paragraph one\n\nParagraph two",
            ),
        ):
            proc = WebProcessor()
            doc = await proc.process(_make_source())

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.WEB
        assert len(doc.chunks) == 2
        assert doc.chunks[0].chunk_type == ChunkType.WEB_CONTENT

    async def test_fetch_failure(self) -> None:
        """fetch_url returns None -> ProcessingError."""
        with patch("trafilatura.fetch_url", return_value=None):
            proc = WebProcessor()
            with pytest.raises(ProcessingError, match="Failed to fetch URL"):
                await proc.process(_make_source())

    async def test_extract_empty(self) -> None:
        """extract returns None -> empty chunks (not an error)."""
        with (
            patch("trafilatura.fetch_url", return_value="<html></html>"),
            patch("trafilatura.extract", return_value=None),
        ):
            proc = WebProcessor()
            doc = await proc.process(_make_source())

        assert doc.chunks == []

    async def test_domain_in_metadata(self) -> None:
        """URL domain extracted to metadata."""
        with (
            patch("trafilatura.fetch_url", return_value="<html>ok</html>"),
            patch("trafilatura.extract", return_value="text"),
        ):
            proc = WebProcessor()
            doc = await proc.process(
                _make_source(url="https://docs.python.org/3/tutorial.html")
            )

        assert doc.metadata["domain"] == "docs.python.org"

    async def test_invalid_source_type(self) -> None:
        """Non-web source_type -> UnsupportedFormatError."""
        proc = WebProcessor()
        with pytest.raises(UnsupportedFormatError, match="expects 'web'"):
            await proc.process(_make_source(source_type="text"))

    async def test_content_snapshot(self) -> None:
        """Raw HTML saved in metadata for later re-processing."""
        raw_html = "<html><body>Raw content</body></html>"
        with (
            patch("trafilatura.fetch_url", return_value=raw_html),
            patch("trafilatura.extract", return_value="Content"),
        ):
            proc = WebProcessor()
            doc = await proc.process(_make_source())

        assert doc.metadata["content_snapshot"] == raw_html

    async def test_chunks_indexed(self) -> None:
        """Multiple paragraphs -> ordered chunks."""
        text = "Para 1\n\nPara 2\n\nPara 3"
        with (
            patch("trafilatura.fetch_url", return_value="<html>ok</html>"),
            patch("trafilatura.extract", return_value=text),
        ):
            proc = WebProcessor()
            doc = await proc.process(_make_source())

        assert len(doc.chunks) == 3
        indices = [c.index for c in doc.chunks]
        assert indices == [0, 1, 2]

    async def test_fetched_at_in_metadata(self) -> None:
        """fetched_at timestamp present in metadata."""
        with (
            patch("trafilatura.fetch_url", return_value="<html>ok</html>"),
            patch("trafilatura.extract", return_value="text"),
        ):
            proc = WebProcessor()
            doc = await proc.process(_make_source())

        assert "fetched_at" in doc.metadata
