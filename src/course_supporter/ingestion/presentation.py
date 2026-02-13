"""Presentation processor for PDF and PPTX files."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from course_supporter.ingestion.base import (
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

SUPPORTED_EXTENSIONS = {".pdf", ".pptx"}


class PresentationProcessor(SourceProcessor):
    """Process PDF and PPTX presentations.

    Extracts text from each slide/page as SLIDE_TEXT chunks.
    Optionally uses Vision LLM (via router) to describe
    slide images as SLIDE_DESCRIPTION chunks.
    """

    async def process(
        self,
        source: SourceMaterial,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        if source.source_type != SourceType.PRESENTATION:
            raise UnsupportedFormatError(
                f"PresentationProcessor expects 'presentation', "
                f"got '{source.source_type}'"
            )

        path = Path(source.source_url)
        ext = path.suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            raise UnsupportedFormatError(
                f"Unsupported presentation format: {ext}. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )

        logger.info(
            "presentation_processing_start",
            source_url=source.source_url,
            format=ext,
        )

        if ext == ".pdf":
            chunks, page_count = await self._process_pdf(path, router=router)
        else:
            chunks, page_count = await self._process_pptx(path)

        logger.info(
            "presentation_processing_done",
            source_url=source.source_url,
            chunk_count=len(chunks),
        )

        return SourceDocument(
            source_type=SourceType.PRESENTATION,
            source_url=source.source_url,
            title=source.filename or path.stem,
            chunks=chunks,
            metadata={"page_count": page_count, "format": ext.lstrip(".")},
        )

    async def _process_pdf(
        self,
        path: Path,
        *,
        router: ModelRouter | None = None,
    ) -> tuple[list[ContentChunk], int]:
        """Extract text (and optionally images) from PDF pages.

        Returns:
            Tuple of (chunks, page_count).
        """
        import fitz

        chunks: list[ContentChunk] = []
        doc = fitz.open(str(path))

        try:
            page_count = len(doc)

            for page_idx in range(page_count):
                page = doc[page_idx]
                slide_number = page_idx + 1
                text = page.get_text().strip()

                if text:
                    chunks.append(
                        ContentChunk(
                            chunk_type=ChunkType.SLIDE_TEXT,
                            text=text,
                            index=slide_number,
                            metadata={"slide_number": slide_number},
                        )
                    )

                # Optional: Vision LLM analysis of slide image
                if router is not None:
                    description = await self._analyze_slide_image(
                        page, slide_number, router=router
                    )
                    if description:
                        chunks.append(
                            ContentChunk(
                                chunk_type=ChunkType.SLIDE_DESCRIPTION,
                                text=description,
                                index=slide_number,
                                metadata={"slide_number": slide_number},
                            )
                        )
        finally:
            doc.close()

        return chunks, page_count

    async def _process_pptx(
        self,
        path: Path,
    ) -> tuple[list[ContentChunk], int]:
        """Extract text from PPTX slides.

        Returns:
            Tuple of (chunks, slide_count).
        """
        from pptx import Presentation

        chunks: list[ContentChunk] = []
        prs = Presentation(str(path))
        slides = list(prs.slides)

        for slide_idx, slide in enumerate(slides):
            slide_number = slide_idx + 1
            texts: list[str] = []

            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        text = paragraph.text.strip()
                        if text:
                            texts.append(text)

            if texts:
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.SLIDE_TEXT,
                        text="\n".join(texts),
                        index=slide_number,
                        metadata={"slide_number": slide_number},
                    )
                )

        return chunks, len(slides)

    async def _analyze_slide_image(
        self,
        page: Any,
        slide_number: int,
        *,
        router: ModelRouter,
    ) -> str | None:
        """Send slide image to Vision LLM for description.

        Returns description string or None if analysis fails.
        Failures are logged but do not crash processing.
        """
        try:
            pixmap = page.get_pixmap(dpi=150)
            _image_bytes = pixmap.tobytes("png")

            response = await router.complete(
                action="presentation_analysis",
                prompt=(
                    f"Describe slide {slide_number}. "
                    "Focus on diagrams, charts, and key visual elements. "
                    "Ignore decorative elements."
                ),
                # TODO: attach image_bytes to the request
                # when router supports multimodal input
            )
            return response.content

        except Exception:
            logger.warning(
                "slide_vision_analysis_failed",
                slide_number=slide_number,
                exc_info=True,
            )
            return None
