"""Tests for local_parse_pdf heavy step (S3-007)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.heavy_steps import ParsePDFParams, PDFPageText
from course_supporter.ingestion.parse_pdf import local_parse_pdf


def _make_fitz_doc(pages: list[MagicMock]) -> MagicMock:
    """Create a mock fitz.Document with indexed page access."""
    mock_doc = MagicMock()
    mock_doc.__len__ = lambda self: len(pages)
    mock_doc.__getitem__ = lambda self, idx: pages[idx]
    return mock_doc


def _make_page(text: str) -> MagicMock:
    page = MagicMock()
    page.get_text.return_value = text
    return page


class TestLocalParsePDF:
    async def test_happy_path(self, tmp_path: object) -> None:
        """Extract text from multi-page PDF."""
        mock_doc = _make_fitz_doc(
            [
                _make_page("Page 1 text"),
                _make_page("Page 2 text"),
            ]
        )

        with (
            patch("course_supporter.ingestion.parse_pdf.Path") as mock_path_cls,
            patch("fitz.open", return_value=mock_doc),
        ):
            mock_path_cls.return_value.exists.return_value = True
            result = await local_parse_pdf("/fake/doc.pdf", ParsePDFParams())

        assert len(result) == 2
        assert result[0] == PDFPageText(page_number=1, text="Page 1 text")
        assert result[1] == PDFPageText(page_number=2, text="Page 2 text")

    async def test_empty_pdf(self) -> None:
        """PDF with no pages returns empty list."""
        mock_doc = _make_fitz_doc([])

        with (
            patch("course_supporter.ingestion.parse_pdf.Path") as mock_path_cls,
            patch("fitz.open", return_value=mock_doc),
        ):
            mock_path_cls.return_value.exists.return_value = True
            result = await local_parse_pdf("/fake/empty.pdf", ParsePDFParams())

        assert result == []

    async def test_whitespace_pages_skipped(self) -> None:
        """Pages with only whitespace are excluded."""
        mock_doc = _make_fitz_doc(
            [
                _make_page("   \n  "),
                _make_page("Real content"),
                _make_page(""),
            ]
        )

        with (
            patch("course_supporter.ingestion.parse_pdf.Path") as mock_path_cls,
            patch("fitz.open", return_value=mock_doc),
        ):
            mock_path_cls.return_value.exists.return_value = True
            result = await local_parse_pdf("/fake/sparse.pdf", ParsePDFParams())

        assert len(result) == 1
        assert result[0].page_number == 2
        assert result[0].text == "Real content"

    async def test_file_not_found(self) -> None:
        """Non-existent file raises ProcessingError."""
        with pytest.raises(ProcessingError, match="not found"):
            await local_parse_pdf("/nonexistent/file.pdf", ParsePDFParams())

    async def test_fitz_not_installed(self) -> None:
        """Missing fitz raises ProcessingError."""
        with (
            patch("course_supporter.ingestion.parse_pdf.Path") as mock_path_cls,
            patch.dict("sys.modules", {"fitz": None}),
        ):
            mock_path_cls.return_value.exists.return_value = True
            with pytest.raises(ProcessingError, match="not installed"):
                await local_parse_pdf("/fake/doc.pdf", ParsePDFParams())

    async def test_corrupt_file(self) -> None:
        """fitz.open failure raises ProcessingError."""
        with (
            patch("course_supporter.ingestion.parse_pdf.Path") as mock_path_cls,
            patch("fitz.open", side_effect=RuntimeError("corrupt")),
        ):
            mock_path_cls.return_value.exists.return_value = True
            with pytest.raises(ProcessingError, match="Failed to open PDF"):
                await local_parse_pdf("/fake/corrupt.pdf", ParsePDFParams())
