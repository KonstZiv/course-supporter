# 📋 S1-014: PresentationProcessor (PDF + PPTX)

## Мета

Реалізувати обробку презентацій: PDF через PyMuPDF (`fitz`), PPTX через `python-pptx`. Витягування тексту зі слайдів → `SLIDE_TEXT` chunks. Опційний аналіз зображень через Vision LLM → `SLIDE_DESCRIPTION` chunks. Graceful degradation при збої LLM.

## Контекст

Залежить від S1-011 (schemas + ABC). Використовує `ModelRouter` з Epic 2 для опційного Vision LLM аналізу. Action `presentation_analysis` вже зареєстрований в `config/models.yaml` з `requires: [vision, structured_output]`. Бібліотеки `pymupdf` та `python-pptx` вже в `pyproject.toml`.

---

## Acceptance Criteria

- [ ] `PresentationProcessor` реалізує `SourceProcessor.process()`
- [ ] PDF: `fitz.open()` → text extraction per page → `SLIDE_TEXT` chunks
- [ ] PPTX: `Presentation()` → text з shapes per slide → `SLIDE_TEXT` chunks
- [ ] Vision LLM (optional): slide image → `SLIDE_DESCRIPTION` chunks
- [ ] Без router → тільки text extraction (no crash)
- [ ] LLM failure → graceful fallback до text-only
- [ ] Непідтримане розширення (.doc, .odp) → `UnsupportedFormatError`
- [ ] Empty PDF/PPTX → empty chunks
- [ ] Correct slide numbering (1-based) у metadata
- [ ] ~10 unit-тестів
- [ ] `make check` проходить

---

## Реалізація

### src/course_supporter/ingestion/presentation.py

```python
"""Presentation processor for PDF and PPTX files."""

from pathlib import Path

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
)

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
        source: "SourceMaterial",
        *,
        router: "ModelRouter | None" = None,
    ) -> SourceDocument:
        if source.source_type != "presentation":
            raise UnsupportedFormatError(
                f"PresentationProcessor expects 'presentation', "
                f"got '{source.source_type}'"
            )

        path = Path(source.source_url)
        ext = path.suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            raise UnsupportedFormatError(
                f"Unsupported presentation format: {ext}. "
                f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
            )

        logger.info(
            "presentation_processing_start",
            source_url=source.source_url,
            format=ext,
        )

        if ext == ".pdf":
            chunks = await self._process_pdf(path, router=router)
            page_count = len(chunks)  # approximate
        else:
            chunks = await self._process_pptx(path, router=router)
            page_count = len(chunks)

        logger.info(
            "presentation_processing_done",
            source_url=source.source_url,
            chunk_count=len(chunks),
        )

        return SourceDocument(
            source_type="presentation",
            source_url=source.source_url,
            title=source.filename or path.stem,
            chunks=chunks,
            metadata={"page_count": page_count, "format": ext.lstrip(".")},
        )

    async def _process_pdf(
        self,
        path: Path,
        *,
        router: "ModelRouter | None" = None,
    ) -> list[ContentChunk]:
        """Extract text (and optionally images) from PDF pages."""
        import fitz  # type: ignore[import-untyped]

        chunks: list[ContentChunk] = []
        doc = fitz.open(str(path))

        try:
            for page_idx, page in enumerate(doc):
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

        return chunks

    async def _process_pptx(
        self,
        path: Path,
        *,
        router: "ModelRouter | None" = None,
    ) -> list[ContentChunk]:
        """Extract text from PPTX slides."""
        from pptx import Presentation  # type: ignore[import-untyped]

        chunks: list[ContentChunk] = []
        prs = Presentation(str(path))

        for slide_idx, slide in enumerate(prs.slides):
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

        return chunks

    async def _analyze_slide_image(
        self,
        page: "fitz.Page",
        slide_number: int,
        *,
        router: "ModelRouter",
    ) -> str | None:
        """Send slide image to Vision LLM for description.

        Returns description string or None if analysis fails.
        Failures are logged but do not crash processing.
        """
        try:
            pixmap = page.get_pixmap(dpi=150)
            image_bytes = pixmap.tobytes("png")

            response = await router.complete(
                action="presentation_analysis",
                prompt=(
                    f"Describe slide {slide_number}. "
                    "Focus on diagrams, charts, and key visual elements. "
                    "Ignore decorative elements."
                ),
                # TODO: attach image_bytes to the request
                # This depends on router supporting multimodal input
            )
            return response.content

        except Exception:
            logger.warning(
                "slide_vision_analysis_failed",
                slide_number=slide_number,
                exc_info=True,
            )
            return None
```

---

## Тести

### tests/unit/test_ingestion/test_presentation.py

```python
"""Tests for PresentationProcessor."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion.base import UnsupportedFormatError
from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.models.source import ChunkType, SourceDocument


def _make_source(
    source_type: str = "presentation",
    url: str = "file:///slides.pdf",
    filename: str = "slides.pdf",
) -> MagicMock:
    source = MagicMock()
    source.source_type = source_type
    source.source_url = url
    source.filename = filename
    return source


class TestPDFProcessing:
    async def test_pdf_text_extraction(self) -> None:
        """Mock fitz → chunks with slide_text type."""
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Slide 1 content"

        mock_doc = MagicMock()
        mock_doc.__iter__ = lambda self: iter([mock_page])
        mock_doc.__enter__ = lambda self: self
        mock_doc.__exit__ = MagicMock(return_value=False)

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source())

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == "presentation"
        assert len(doc.chunks) >= 1
        assert doc.chunks[0].chunk_type == ChunkType.SLIDE_TEXT

    async def test_pdf_with_vision_analysis(self) -> None:
        """Mock router.complete → slide_description chunks."""
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Text"
        mock_page.get_pixmap.return_value = MagicMock(
            tobytes=MagicMock(return_value=b"png_data")
        )

        mock_doc = MagicMock()
        mock_doc.__iter__ = lambda self: iter([mock_page])

        router = AsyncMock()
        router.complete.return_value = MagicMock(content="Diagram showing flow")

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source(), router=router)

        desc_chunks = [c for c in doc.chunks if c.chunk_type == ChunkType.SLIDE_DESCRIPTION]
        assert len(desc_chunks) == 1
        assert "Diagram" in desc_chunks[0].text

    async def test_empty_pdf(self) -> None:
        """No pages → empty chunks."""
        mock_doc = MagicMock()
        mock_doc.__iter__ = lambda self: iter([])

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source())

        assert doc.chunks == []

    async def test_vision_failure_graceful(self) -> None:
        """LLM fails → text-only chunks (no crash)."""
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Text content"
        mock_page.get_pixmap.side_effect = RuntimeError("Vision failed")

        mock_doc = MagicMock()
        mock_doc.__iter__ = lambda self: iter([mock_page])

        router = AsyncMock()

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source(), router=router)

        # Should still have text chunk despite vision failure
        text_chunks = [c for c in doc.chunks if c.chunk_type == ChunkType.SLIDE_TEXT]
        assert len(text_chunks) == 1


class TestPPTXProcessing:
    async def test_pptx_text_extraction(self) -> None:
        """Mock Presentation → chunks from shapes."""
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

    async def test_pptx_without_router(self) -> None:
        """No router → no vision analysis, only text."""
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
        """No slides → empty chunks."""
        mock_prs = MagicMock()
        mock_prs.slides = []

        with patch("pptx.Presentation", return_value=mock_prs):
            proc = PresentationProcessor()
            doc = await proc.process(
                _make_source(url="file:///s.pptx", filename="s.pptx")
            )

        assert doc.chunks == []


class TestPresentationProcessorValidation:
    async def test_unsupported_extension(self) -> None:
        """.doc → UnsupportedFormatError."""
        proc = PresentationProcessor()
        with pytest.raises(UnsupportedFormatError, match="Unsupported presentation format"):
            await proc.process(
                _make_source(url="file:///s.doc", filename="s.doc")
            )

    async def test_slide_numbering(self) -> None:
        """Chunk index matches 1-based slide number."""
        mock_pages = []
        for i in range(3):
            page = MagicMock()
            page.get_text.return_value = f"Slide {i + 1}"
            mock_pages.append(page)

        mock_doc = MagicMock()
        mock_doc.__iter__ = lambda self: iter(mock_pages)

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source())

        for i, chunk in enumerate(doc.chunks):
            assert chunk.metadata["slide_number"] == i + 1
            assert chunk.index == i + 1

    async def test_source_document_metadata(self) -> None:
        """page_count and format in metadata."""
        mock_doc = MagicMock()
        mock_doc.__iter__ = lambda self: iter([])

        with patch("fitz.open", return_value=mock_doc):
            proc = PresentationProcessor()
            doc = await proc.process(_make_source())

        assert doc.metadata["format"] == "pdf"
        assert "page_count" in doc.metadata
```

---

## Структура файлів

```
src/course_supporter/ingestion/
├── presentation.py          # PresentationProcessor

tests/unit/test_ingestion/
└── test_presentation.py     # ~10 tests
```

---

## Кроки виконання

1. Переконатися, що S1-011 завершено
2. Реалізувати `PresentationProcessor` в `ingestion/presentation.py`
3. Створити `tests/unit/test_ingestion/test_presentation.py`
4. `make check`

---

## Примітки

- **Vision LLM integration**: повна multimodal підтримка (image bytes → router) потребує розширення `ModelRouter` або прямого виклику Gemini SDK. Поточна реалізація — placeholder з TODO.
- **fitz vs PyMuPDF**: `import fitz` — це `pymupdf` пакет. В `mypy` конфігу вже є `ignore_missing_imports` для `fitz`.
- **Graceful degradation**: якщо Vision LLM fails → логуємо warning, повертаємо тільки text chunks. Ніколи не crash через опційну функцію.
- **1-based slide numbering**: слайди нумеруються з 1 (як у презентації), не з 0.
- **PPTX shapes**: не всі shapes мають `text_frame`. Фільтруємо через `shape.has_text_frame`.
