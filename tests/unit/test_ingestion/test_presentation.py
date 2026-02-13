"""Tests for PresentationProcessor."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion.base import UnsupportedFormatError
from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.models.source import ChunkType, SourceDocument, SourceType


def _make_source(
    source_type: str = "presentation",
    url: str = "file:///slides.pdf",
    filename: str = "slides.pdf",
) -> MagicMock:
    """Create a mock SourceMaterial."""
    source = MagicMock()
    source.source_type = source_type
    source.source_url = url
    source.filename = filename
    return source


def _make_fitz_doc(pages: list[MagicMock]) -> MagicMock:
    """Create a mock fitz.Document with indexed page access."""
    mock_doc = MagicMock()
    mock_doc.__len__ = lambda self: len(pages)
    mock_doc.__getitem__ = lambda self, idx: pages[idx]
    return mock_doc


class TestPDFProcessing:
    async def test_text_extraction(self) -> None:
        """Mock fitz -> chunks with SLIDE_TEXT type."""
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Slide 1 content"

        mock_doc = _make_fitz_doc([mock_page])

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source())

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.PRESENTATION
        assert len(doc.chunks) == 1
        assert doc.chunks[0].chunk_type == ChunkType.SLIDE_TEXT
        assert doc.chunks[0].text == "Slide 1 content"

    async def test_with_vision_analysis(self) -> None:
        """Mock router.complete -> SLIDE_DESCRIPTION chunks."""
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Text"
        mock_page.get_pixmap.return_value = MagicMock(
            tobytes=MagicMock(return_value=b"png_data")
        )

        mock_doc = _make_fitz_doc([mock_page])

        router = AsyncMock()
        router.complete.return_value = MagicMock(content="Diagram showing flow")

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source(), router=router)

        desc_chunks = [
            c for c in doc.chunks if c.chunk_type == ChunkType.SLIDE_DESCRIPTION
        ]
        assert len(desc_chunks) == 1
        assert "Diagram" in desc_chunks[0].text

    async def test_empty_pdf(self) -> None:
        """No pages -> empty chunks."""
        mock_doc = _make_fitz_doc([])

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source())

        assert doc.chunks == []
        assert doc.metadata["page_count"] == 0

    async def test_vision_failure_graceful(self) -> None:
        """LLM fails -> text-only chunks (no crash)."""
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Text content"
        mock_page.get_pixmap.side_effect = RuntimeError("Vision failed")

        mock_doc = _make_fitz_doc([mock_page])

        router = AsyncMock()

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source(), router=router)

        text_chunks = [c for c in doc.chunks if c.chunk_type == ChunkType.SLIDE_TEXT]
        assert len(text_chunks) == 1
        desc_chunks = [
            c for c in doc.chunks if c.chunk_type == ChunkType.SLIDE_DESCRIPTION
        ]
        assert len(desc_chunks) == 0

    async def test_empty_page_text_skipped(self) -> None:
        """Pages with only whitespace produce no chunks."""
        mock_page = MagicMock()
        mock_page.get_text.return_value = "   \n  "

        mock_doc = _make_fitz_doc([mock_page])

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source())

        assert doc.chunks == []
        assert doc.metadata["page_count"] == 1


class TestPPTXProcessing:
    async def test_text_extraction(self) -> None:
        """Mock Presentation -> chunks from shapes."""
        mock_para = MagicMock()
        mock_para.text = "Slide content"

        mock_frame = MagicMock()
        mock_frame.paragraphs = [mock_para]

        mock_shape = MagicMock()
        mock_shape.has_text_frame = True
        mock_shape.text_frame = mock_frame

        mock_slide = MagicMock()
        mock_slide.shapes = [mock_shape]

        mock_prs = MagicMock()
        mock_prs.slides = [mock_slide]

        with patch("pptx.Presentation", return_value=mock_prs):
            proc = PresentationProcessor()
            doc = await proc.process(
                _make_source(url="file:///s.pptx", filename="s.pptx")
            )

        assert len(doc.chunks) == 1
        assert doc.chunks[0].chunk_type == ChunkType.SLIDE_TEXT

    async def test_without_router(self) -> None:
        """No router -> no vision analysis, only text."""
        mock_prs = MagicMock()
        mock_prs.slides = []

        with patch("pptx.Presentation", return_value=mock_prs):
            proc = PresentationProcessor()
            doc = await proc.process(
                _make_source(url="file:///s.pptx", filename="s.pptx"),
                router=None,
            )

        assert doc.chunks == []

    async def test_empty_pptx(self) -> None:
        """No slides -> empty chunks."""
        mock_prs = MagicMock()
        mock_prs.slides = []

        with patch("pptx.Presentation", return_value=mock_prs):
            proc = PresentationProcessor()
            doc = await proc.process(
                _make_source(url="file:///s.pptx", filename="s.pptx")
            )

        assert doc.chunks == []
        assert doc.metadata["page_count"] == 0


class TestPresentationProcessorValidation:
    async def test_unsupported_extension(self) -> None:
        """.doc -> UnsupportedFormatError."""
        proc = PresentationProcessor()
        with pytest.raises(
            UnsupportedFormatError, match="Unsupported presentation format"
        ):
            await proc.process(_make_source(url="file:///s.doc", filename="s.doc"))

    async def test_invalid_source_type(self) -> None:
        """Non-presentation source_type -> UnsupportedFormatError."""
        proc = PresentationProcessor()
        with pytest.raises(UnsupportedFormatError, match="expects 'presentation'"):
            await proc.process(_make_source(source_type="video"))

    async def test_slide_numbering(self) -> None:
        """Chunk index and metadata use 1-based slide numbers."""
        mock_pages = []
        for i in range(3):
            page = MagicMock()
            page.get_text.return_value = f"Slide {i + 1}"
            mock_pages.append(page)

        mock_doc = _make_fitz_doc(mock_pages)

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source())

        for i, chunk in enumerate(doc.chunks):
            assert chunk.metadata["slide_number"] == i + 1
            assert chunk.index == i + 1

    async def test_source_document_metadata(self) -> None:
        """page_count and format in metadata."""
        mock_doc = _make_fitz_doc([])

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source())

        assert doc.metadata["format"] == "pdf"
        assert doc.metadata["page_count"] == 0

    async def test_pptx_metadata(self) -> None:
        """PPTX format in metadata."""
        mock_prs = MagicMock()
        mock_prs.slides = []

        with patch("pptx.Presentation", return_value=mock_prs):
            proc = PresentationProcessor()
            doc = await proc.process(
                _make_source(url="file:///s.pptx", filename="s.pptx")
            )

        assert doc.metadata["format"] == "pptx"
