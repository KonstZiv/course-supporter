"""Tests for TextProcessor."""

from unittest.mock import MagicMock, patch

import pytest

from course_supporter.ingestion.base import UnsupportedFormatError
from course_supporter.ingestion.text import TextProcessor
from course_supporter.models.source import ChunkType, SourceDocument, SourceType


def _make_source(
    source_type: str = "text",
    url: str = "file:///doc.md",
    filename: str = "doc.md",
) -> MagicMock:
    """Create a mock SourceMaterial."""
    source = MagicMock()
    source.source_type = source_type
    source.source_url = url
    source.filename = filename
    return source


class TestMarkdownProcessing:
    async def test_markdown_headings(self) -> None:
        """# H1 → HEADING chunk with level=1."""
        md_content = "# Title\n\nSome text\n\n## Section\n\nMore text"

        with patch("pathlib.Path.read_text", return_value=md_content):
            proc = TextProcessor()
            doc = await proc.process(_make_source())

        assert isinstance(doc, SourceDocument)
        headings = [c for c in doc.chunks if c.chunk_type == ChunkType.HEADING]
        assert len(headings) == 2
        assert headings[0].text == "Title"
        assert headings[0].metadata["level"] == 1
        assert headings[1].text == "Section"
        assert headings[1].metadata["level"] == 2

    async def test_markdown_paragraphs(self) -> None:
        """Text between headings → PARAGRAPH chunks."""
        md_content = "# Title\n\nParagraph one\n\n## Sub\n\nParagraph two"

        with patch("pathlib.Path.read_text", return_value=md_content):
            proc = TextProcessor()
            doc = await proc.process(_make_source())

        paragraphs = [c for c in doc.chunks if c.chunk_type == ChunkType.PARAGRAPH]
        assert len(paragraphs) == 2


class TestDocxProcessing:
    async def test_docx_extraction(self) -> None:
        """Mock docx.Document → correct chunks."""
        mock_heading = MagicMock()
        mock_heading.text = "Chapter 1"
        mock_heading.style.name = "Heading 1"

        mock_para = MagicMock()
        mock_para.text = "Body text"
        mock_para.style.name = "Normal"

        mock_doc = MagicMock()
        mock_doc.paragraphs = [mock_heading, mock_para]

        with patch("docx.Document", return_value=mock_doc):
            proc = TextProcessor()
            doc = await proc.process(
                _make_source(url="file:///d.docx", filename="d.docx")
            )

        assert len(doc.chunks) == 2
        assert doc.chunks[0].chunk_type == ChunkType.HEADING
        assert doc.chunks[1].chunk_type == ChunkType.PARAGRAPH

    async def test_docx_heading_styles(self) -> None:
        """'Heading 1' style → level=1, 'Heading 3' → level=3."""
        mock_h1 = MagicMock()
        mock_h1.text = "H1"
        mock_h1.style.name = "Heading 1"

        mock_h3 = MagicMock()
        mock_h3.text = "H3"
        mock_h3.style.name = "Heading 3"

        mock_doc = MagicMock()
        mock_doc.paragraphs = [mock_h1, mock_h3]

        with patch("docx.Document", return_value=mock_doc):
            proc = TextProcessor()
            doc = await proc.process(
                _make_source(url="file:///d.docx", filename="d.docx")
            )

        assert doc.chunks[0].metadata["level"] == 1
        assert doc.chunks[1].metadata["level"] == 3


class TestHTMLProcessing:
    async def test_html_extraction(self) -> None:
        """BS4 parses headings and paragraphs."""
        html = "<html><body><h1>Title</h1><p>Text</p><h2>Sub</h2></body></html>"

        with patch("pathlib.Path.read_text", return_value=html):
            proc = TextProcessor()
            doc = await proc.process(
                _make_source(url="file:///p.html", filename="p.html")
            )

        assert len(doc.chunks) == 3
        assert doc.chunks[0].chunk_type == ChunkType.HEADING
        assert doc.chunks[0].metadata["level"] == 1
        assert doc.chunks[1].chunk_type == ChunkType.PARAGRAPH
        assert doc.chunks[2].chunk_type == ChunkType.HEADING
        assert doc.chunks[2].metadata["level"] == 2


class TestPlainTextProcessing:
    async def test_plain_text(self) -> None:
        """.txt → single PARAGRAPH chunk."""
        with patch("pathlib.Path.read_text", return_value="Just plain text"):
            proc = TextProcessor()
            doc = await proc.process(
                _make_source(url="file:///f.txt", filename="f.txt")
            )

        assert len(doc.chunks) == 1
        assert doc.chunks[0].chunk_type == ChunkType.PARAGRAPH

    async def test_empty_file(self) -> None:
        """Empty content → empty chunks."""
        with patch("pathlib.Path.read_text", return_value=""):
            proc = TextProcessor()
            doc = await proc.process(
                _make_source(url="file:///f.txt", filename="f.txt")
            )

        assert doc.chunks == []


class TestTextProcessorValidation:
    async def test_unsupported_extension(self) -> None:
        """.rtf → UnsupportedFormatError."""
        proc = TextProcessor()
        with pytest.raises(UnsupportedFormatError, match="Unsupported text format"):
            await proc.process(_make_source(url="file:///f.rtf", filename="f.rtf"))

    async def test_invalid_source_type(self) -> None:
        """Non-text source_type → UnsupportedFormatError."""
        proc = TextProcessor()
        with pytest.raises(UnsupportedFormatError, match="expects 'text'"):
            await proc.process(_make_source(source_type="video"))

    async def test_chunk_ordering(self) -> None:
        """Chunks indexed sequentially."""
        md_content = "# H1\n\nPara\n\n## H2\n\nPara2"

        with patch("pathlib.Path.read_text", return_value=md_content):
            proc = TextProcessor()
            doc = await proc.process(_make_source())

        indices = [c.index for c in doc.chunks]
        assert indices == list(range(len(indices)))

    async def test_source_document_type(self) -> None:
        """Output source_type is SourceType.TEXT."""
        with patch("pathlib.Path.read_text", return_value="text"):
            proc = TextProcessor()
            doc = await proc.process(
                _make_source(url="file:///f.txt", filename="f.txt")
            )

        assert doc.source_type == SourceType.TEXT
