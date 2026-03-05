"""Tests for PresentationProcessor."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion.base import UnsupportedFormatError
from course_supporter.ingestion.heavy_steps import PDFPageText, SlideDescription
from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.models.source import ChunkType, SourceDocument, SourceType


def _make_source(
    source_type: str = "presentation",
    url: str = "file:///slides.pdf",
    filename: str = "slides.pdf",
) -> MagicMock:
    """Create a mock MaterialEntry."""
    source = MagicMock()
    source.source_type = source_type
    source.source_url = url
    source.filename = filename
    return source


def _mock_parse_pdf(pages: list[PDFPageText]) -> AsyncMock:
    """Create a mock ParsePDFFunc returning given pages."""
    return AsyncMock(return_value=pages)


class TestPDFProcessing:
    async def test_text_extraction(self) -> None:
        """parse_pdf_func -> chunks with SLIDE_TEXT type."""
        mock_parse = _mock_parse_pdf(
            [
                PDFPageText(page_number=1, text="Slide 1 content"),
            ]
        )
        proc = PresentationProcessor(parse_pdf_func=mock_parse)
        doc = await proc.process(_make_source())

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.PRESENTATION
        assert len(doc.chunks) == 1
        assert doc.chunks[0].chunk_type == ChunkType.SLIDE_TEXT
        assert doc.chunks[0].text == "Slide 1 content"

    async def test_with_vision_analysis(self) -> None:
        """Injected describe_slides_func -> SLIDE_DESCRIPTION chunks."""
        mock_parse = _mock_parse_pdf(
            [
                PDFPageText(page_number=1, text="Text"),
            ]
        )
        mock_describe = AsyncMock(
            return_value=[
                SlideDescription(slide_number=1, description="Diagram showing flow"),
            ]
        )

        proc = PresentationProcessor(
            parse_pdf_func=mock_parse,
            describe_slides_func=mock_describe,
        )
        doc = await proc.process(_make_source())

        desc_chunks = [
            c for c in doc.chunks if c.chunk_type == ChunkType.SLIDE_DESCRIPTION
        ]
        assert len(desc_chunks) == 1
        assert "Diagram" in desc_chunks[0].text

    async def test_empty_pdf(self) -> None:
        """No pages -> empty chunks."""
        mock_parse = _mock_parse_pdf([])
        proc = PresentationProcessor(parse_pdf_func=mock_parse)
        doc = await proc.process(_make_source())

        assert doc.chunks == []
        assert doc.metadata["page_count"] == 0

    async def test_vision_failure_graceful(self) -> None:
        """describe_slides_func raises -> text-only chunks (no crash)."""
        mock_parse = _mock_parse_pdf(
            [
                PDFPageText(page_number=1, text="Text content"),
            ]
        )
        mock_describe = AsyncMock(side_effect=RuntimeError("Vision failed"))

        proc = PresentationProcessor(
            parse_pdf_func=mock_parse,
            describe_slides_func=mock_describe,
        )
        doc = await proc.process(_make_source())

        text_chunks = [c for c in doc.chunks if c.chunk_type == ChunkType.SLIDE_TEXT]
        assert len(text_chunks) == 1
        desc_chunks = [
            c for c in doc.chunks if c.chunk_type == ChunkType.SLIDE_DESCRIPTION
        ]
        assert len(desc_chunks) == 0

    async def test_no_describe_func_text_only(self) -> None:
        """No describe_slides_func -> text-only chunks (no crash)."""
        mock_parse = _mock_parse_pdf(
            [
                PDFPageText(page_number=1, text="Text content"),
            ]
        )
        proc = PresentationProcessor(parse_pdf_func=mock_parse)
        doc = await proc.process(_make_source())

        text_chunks = [c for c in doc.chunks if c.chunk_type == ChunkType.SLIDE_TEXT]
        assert len(text_chunks) == 1
        desc_chunks = [
            c for c in doc.chunks if c.chunk_type == ChunkType.SLIDE_DESCRIPTION
        ]
        assert len(desc_chunks) == 0

    async def test_empty_page_text_skipped(self) -> None:
        """Pages with only whitespace are not returned by parse_pdf."""
        mock_parse = _mock_parse_pdf([])  # parse_pdf strips empty pages
        proc = PresentationProcessor(parse_pdf_func=mock_parse)
        doc = await proc.process(_make_source())

        assert doc.chunks == []
        assert doc.metadata["page_count"] == 0

    async def test_interleaved_chunk_order(self) -> None:
        """Chunks are interleaved: Text1, Desc1, Text2, Desc2."""
        mock_parse = _mock_parse_pdf(
            [
                PDFPageText(page_number=1, text="Slide 1"),
                PDFPageText(page_number=2, text="Slide 2"),
            ]
        )
        mock_describe = AsyncMock(
            return_value=[
                SlideDescription(slide_number=1, description="Desc 1"),
                SlideDescription(slide_number=2, description="Desc 2"),
            ]
        )

        proc = PresentationProcessor(
            parse_pdf_func=mock_parse,
            describe_slides_func=mock_describe,
        )
        doc = await proc.process(_make_source())

        assert len(doc.chunks) == 4
        assert doc.chunks[0].chunk_type == ChunkType.SLIDE_TEXT
        assert doc.chunks[0].index == 1
        assert doc.chunks[1].chunk_type == ChunkType.SLIDE_DESCRIPTION
        assert doc.chunks[1].index == 1
        assert doc.chunks[2].chunk_type == ChunkType.SLIDE_TEXT
        assert doc.chunks[2].index == 2
        assert doc.chunks[3].chunk_type == ChunkType.SLIDE_DESCRIPTION
        assert doc.chunks[3].index == 2

    async def test_parse_pdf_func_called_with_path_and_params(self) -> None:
        """parse_pdf_func receives str(path) and ParsePDFParams."""
        mock_parse = _mock_parse_pdf([])

        proc = PresentationProcessor(parse_pdf_func=mock_parse)
        await proc.process(_make_source(url="file:///slides.pdf"))

        mock_parse.assert_awaited_once()
        args = mock_parse.call_args
        assert args[0][0] == str(Path("file:///slides.pdf"))

    async def test_default_parse_pdf_func(self) -> None:
        """Without injected parse_pdf, falls back to local_parse_pdf."""
        proc = PresentationProcessor()
        from course_supporter.ingestion.parse_pdf import local_parse_pdf

        assert proc._parse_pdf_func is local_parse_pdf


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

    async def test_without_describe_func(self) -> None:
        """No describe_slides_func -> no vision analysis, only text."""
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
        mock_parse = _mock_parse_pdf(
            [
                PDFPageText(page_number=1, text="Slide 1"),
                PDFPageText(page_number=2, text="Slide 2"),
                PDFPageText(page_number=3, text="Slide 3"),
            ]
        )
        proc = PresentationProcessor(parse_pdf_func=mock_parse)
        doc = await proc.process(_make_source())

        for i, chunk in enumerate(doc.chunks):
            assert chunk.metadata["slide_number"] == i + 1
            assert chunk.index == i + 1

    async def test_source_document_metadata(self) -> None:
        """page_count and format in metadata."""
        mock_parse = _mock_parse_pdf([])
        proc = PresentationProcessor(parse_pdf_func=mock_parse)
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
