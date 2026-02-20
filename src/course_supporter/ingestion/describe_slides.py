"""Local slide description via Vision LLM — heavy step implementation.

Standalone async function that renders PDF pages to images and sends
them to a Vision LLM for description. Conforms to ``DescribeSlidesFunc``
protocol from :mod:`course_supporter.ingestion.heavy_steps`.

No DB, S3, or ORM dependencies — pure pdf-in, descriptions-out.
ModelRouter is injected via keyword argument; the factory (S2-036)
will bind it with ``functools.partial`` to match the protocol signature.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.heavy_steps import (
    DescribeSlidesParams,
    SlideDescription,
)

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter


async def local_describe_slides(
    pdf_path: str,
    params: DescribeSlidesParams,
    *,
    router: ModelRouter,
) -> list[SlideDescription]:
    """Describe PDF slides using a Vision LLM.

    Renders each PDF page to a PNG image at the configured DPI
    (sequential — fitz is not thread-safe for concurrent document access),
    then sends all images to the Vision LLM in parallel via asyncio.gather
    (I/O-bound LLM calls benefit from concurrency).

    Individual slide failures are logged and skipped — they do not
    crash the entire batch.

    Args:
        pdf_path: Path to a PDF file on local disk.
        params: Description parameters (DPI, prompt).
        router: ModelRouter instance for Vision LLM calls.

    Returns:
        List of slide descriptions for pages that were successfully analyzed.

    Raises:
        ProcessingError: If fitz is not installed, file not found,
            or the PDF cannot be opened.
    """
    logger = structlog.get_logger().bind(
        pdf_path=pdf_path,
        dpi=params.dpi,
    )
    logger.info("slide_description_start")

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
        page_count = len(doc)
        logger.info("slide_description_pdf_opened", page_count=page_count)

        # Phase 1: render all pages to PNG (sequential — fitz not thread-safe)
        page_images: list[tuple[int, bytes]] = []
        for page_idx in range(page_count):
            image_bytes = await loop.run_in_executor(
                None, _render_page_to_png, doc, page_idx, params.dpi
            )
            page_images.append((page_idx + 1, image_bytes))
    finally:
        doc.close()

    # Phase 2: send all images to Vision LLM in parallel (I/O-bound)
    tasks = [
        _describe_with_llm(
            slide_number=slide_number,
            image_bytes=image_bytes,
            params=params,
            router=router,
            logger=logger,
        )
        for slide_number, image_bytes in page_images
    ]
    results = await asyncio.gather(*tasks)

    descriptions = [d for d in results if d is not None]

    logger.info(
        "slide_description_done",
        total_pages=page_count,
        described_count=len(descriptions),
    )
    return descriptions


async def _describe_with_llm(
    *,
    slide_number: int,
    image_bytes: bytes,
    params: DescribeSlidesParams,
    router: ModelRouter,
    logger: structlog.stdlib.BoundLogger,
) -> SlideDescription | None:
    """Send a single slide image to Vision LLM for description.

    Returns None if the call fails (logged, not raised).
    """
    try:
        response = await router.complete(
            action="presentation_analysis",
            prompt=params.prompt,
            contents=[image_bytes],
        )

        text = (response.content or "").strip()
        if not text:
            logger.warning(
                "slide_description_empty_response",
                slide_number=slide_number,
            )
            return None

        return SlideDescription(
            slide_number=slide_number,
            description=text,
        )
    except Exception:
        logger.warning(
            "slide_description_failed",
            slide_number=slide_number,
            exc_info=True,
        )
        return None


def _render_page_to_png(doc: object, page_idx: int, dpi: int) -> bytes:
    """Render a PDF page to PNG bytes (sync, runs in executor)."""
    page = doc[page_idx]  # type: ignore[index]
    pixmap = page.get_pixmap(dpi=dpi)
    return bytes(pixmap.tobytes("png"))
