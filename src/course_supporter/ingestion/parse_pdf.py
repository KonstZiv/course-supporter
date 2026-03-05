"""Local PDF text extraction via PyMuPDF (fitz) — heavy step implementation.

Standalone async function that extracts text from each PDF page.
Conforms to ``ParsePDFFunc`` protocol from
:mod:`course_supporter.ingestion.heavy_steps`.

No DB, S3, or ORM dependencies — pure pdf-in, page-texts-out.
Can be swapped for a Lambda-based or OCR implementation later.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.heavy_steps import ParsePDFParams, PDFPageText


async def local_parse_pdf(
    pdf_path: str,
    params: ParsePDFParams,
) -> list[PDFPageText]:
    """Extract text from each page of a PDF file.

    Runs fitz operations in a thread-pool executor to avoid blocking
    the event loop (fitz is synchronous C code). Pages are processed
    sequentially since fitz is not thread-safe for concurrent document
    access.

    Args:
        pdf_path: Path to a PDF file on local disk.
        params: Extraction parameters (ocr_enabled — reserved for future use).

    Returns:
        List of PDFPageText for every page that has text content.

    Raises:
        ProcessingError: If fitz is not installed, file not found,
            or the PDF cannot be opened.
    """
    logger = structlog.get_logger().bind(pdf_path=pdf_path)
    logger.info("parse_pdf_start")

    if not Path(pdf_path).exists():  # noqa: ASYNC240
        raise ProcessingError(f"PDF file not found: {pdf_path}")

    try:
        import fitz
    except ImportError:
        raise ProcessingError(
            "PyMuPDF (fitz) is not installed. Install with: uv sync --extra media"
        ) from None

    loop = asyncio.get_running_loop()

    try:
        doc = await loop.run_in_executor(None, fitz.open, str(pdf_path))
    except Exception as exc:
        raise ProcessingError(f"Failed to open PDF: {exc}") from exc

    try:
        pages = await loop.run_in_executor(None, _extract_pages, doc)
    finally:
        doc.close()

    logger.info("parse_pdf_done", page_count=len(pages))
    return pages


def _extract_pages(doc: object) -> list[PDFPageText]:
    """Extract text from all pages (sync, runs in executor)."""
    result: list[PDFPageText] = []
    page_count: int = len(doc)  # type: ignore[arg-type]
    for page_idx in range(page_count):
        page = doc[page_idx]  # type: ignore[index]
        text = page.get_text().strip()
        if text:
            result.append(PDFPageText(page_number=page_idx + 1, text=text))
    return result
