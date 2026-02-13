# 📋 S1-016: WebProcessor (URL → trafilatura)

## Мета

Реалізувати обробку веб-сторінок: fetch HTML через `trafilatura`, витягти основний контент, зберегти raw HTML як content snapshot для можливої переобробки. Без LLM — pure extraction.

## Контекст

Залежить від S1-011 (schemas + ABC). Не потребує `ModelRouter`. Бібліотека `trafilatura` вже в `pyproject.toml`. `trafilatura` — specialized lib для web content extraction (краще за BS4 для статей/блогів).

---

## Acceptance Criteria

- [ ] `WebProcessor` реалізує `SourceProcessor.process()`
- [ ] `trafilatura.fetch_url()` отримує HTML
- [ ] `trafilatura.extract()` витягує основний контент
- [ ] Raw HTML зберігається в `metadata["content_snapshot"]`
- [ ] Domain витягнуто в `metadata["domain"]`
- [ ] `fetched_at` timestamp у metadata
- [ ] Fetch failure (returns None) → `ProcessingError`
- [ ] Extract returns None → empty chunks (не error)
- [ ] Non-web source_type → `UnsupportedFormatError`
- [ ] ~7 unit-тестів
- [ ] `make check` проходить

---

## Реалізація

### src/course_supporter/ingestion/web.py

```python
"""Web processor using trafilatura for content extraction."""

from datetime import datetime
from urllib.parse import urlparse

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


class WebProcessor(SourceProcessor):
    """Process web pages by fetching HTML and extracting content.

    Uses trafilatura for intelligent content extraction
    (article text, removing boilerplate/navigation).
    Raw HTML is saved as content_snapshot for re-processing.
    """

    async def process(
        self,
        source: "SourceMaterial",
        *,
        router: "ModelRouter | None" = None,
    ) -> SourceDocument:
        if source.source_type != "web":
            raise UnsupportedFormatError(
                f"WebProcessor expects 'web', got '{source.source_type}'"
            )

        url = source.source_url
        parsed_url = urlparse(url)
        domain = parsed_url.netloc

        logger.info("web_processing_start", url=url, domain=domain)

        # 1. Fetch HTML
        html = await self._fetch_html(url)

        # 2. Extract content
        extracted = self._extract_content(html)

        # 3. Split into chunks
        chunks = self._text_to_chunks(extracted) if extracted else []

        fetched_at = datetime.now().isoformat()

        logger.info(
            "web_processing_done",
            url=url,
            chunk_count=len(chunks),
        )

        return SourceDocument(
            source_type="web",
            source_url=url,
            title=source.filename or domain,
            chunks=chunks,
            metadata={
                "domain": domain,
                "fetched_at": fetched_at,
                "content_snapshot": html,
            },
        )

    @staticmethod
    async def _fetch_html(url: str) -> str:
        """Fetch HTML from URL using trafilatura.

        Args:
            url: The URL to fetch.

        Returns:
            Raw HTML string.

        Raises:
            ProcessingError: If fetch fails (returns None).
        """
        import trafilatura  # type: ignore[import-untyped]

        html = trafilatura.fetch_url(url)
        if html is None:
            raise ProcessingError(
                f"Failed to fetch URL: {url}. "
                "The page may be unreachable or blocked."
            )
        return html

    @staticmethod
    def _extract_content(html: str) -> str | None:
        """Extract main content from HTML using trafilatura.

        Args:
            html: Raw HTML string.

        Returns:
            Extracted text content, or None if extraction fails.
        """
        import trafilatura  # type: ignore[import-untyped]

        return trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
        )

    @staticmethod
    def _text_to_chunks(text: str) -> list[ContentChunk]:
        """Split extracted text into content chunks.

        Splits on double newlines to create paragraph-like chunks.
        """
        chunks: list[ContentChunk] = []
        paragraphs = text.strip().split("\n\n")

        for idx, para in enumerate(paragraphs):
            para = para.strip()
            if not para:
                continue
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.WEB_CONTENT,
                    text=para,
                    index=idx,
                )
            )

        return chunks
```

---

## Тести

### tests/unit/test_ingestion/test_web.py

```python
"""Tests for WebProcessor."""

from unittest.mock import MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.web import WebProcessor
from course_supporter.models.source import ChunkType, SourceDocument


def _make_source(
    source_type: str = "web",
    url: str = "https://example.com/article",
    filename: str | None = None,
) -> MagicMock:
    source = MagicMock()
    source.source_type = source_type
    source.source_url = url
    source.filename = filename
    return source


class TestWebProcessor:
    async def test_web_fetch_success(self) -> None:
        """Mock trafilatura → SourceDocument with WEB_CONTENT chunks."""
        with (
            patch("trafilatura.fetch_url", return_value="<html>content</html>"),
            patch("trafilatura.extract", return_value="Extracted paragraph one\n\nParagraph two"),
        ):
            proc = WebProcessor()
            doc = await proc.process(_make_source())

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == "web"
        assert len(doc.chunks) == 2
        assert doc.chunks[0].chunk_type == ChunkType.WEB_CONTENT

    async def test_web_fetch_failure(self) -> None:
        """fetch_url returns None → ProcessingError."""
        with patch("trafilatura.fetch_url", return_value=None):
            proc = WebProcessor()
            with pytest.raises(ProcessingError, match="Failed to fetch URL"):
                await proc.process(_make_source())

    async def test_web_extract_empty(self) -> None:
        """extract returns None → empty chunks (not an error)."""
        with (
            patch("trafilatura.fetch_url", return_value="<html></html>"),
            patch("trafilatura.extract", return_value=None),
        ):
            proc = WebProcessor()
            doc = await proc.process(_make_source())

        assert doc.chunks == []

    async def test_web_domain_in_metadata(self) -> None:
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

    async def test_web_invalid_source_type(self) -> None:
        """Non-web source_type → UnsupportedFormatError."""
        proc = WebProcessor()
        with pytest.raises(UnsupportedFormatError, match="expects 'web'"):
            await proc.process(_make_source(source_type="text"))

    async def test_web_content_snapshot(self) -> None:
        """Raw HTML saved in metadata for later re-processing."""
        raw_html = "<html><body>Raw content</body></html>"
        with (
            patch("trafilatura.fetch_url", return_value=raw_html),
            patch("trafilatura.extract", return_value="Content"),
        ):
            proc = WebProcessor()
            doc = await proc.process(_make_source())

        assert doc.metadata["content_snapshot"] == raw_html

    async def test_web_chunks_indexed(self) -> None:
        """Multiple paragraphs → ordered chunks."""
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
```

---

## Структура файлів

```
src/course_supporter/ingestion/
├── web.py                   # WebProcessor

tests/unit/test_ingestion/
└── test_web.py              # ~7 tests
```

---

## Кроки виконання

1. Переконатися, що S1-011 завершено
2. Реалізувати `WebProcessor` в `ingestion/web.py`
3. Створити `tests/unit/test_ingestion/test_web.py`
4. `make check`

---

## Примітки

- **trafilatura vs BeautifulSoup**: `trafilatura` спеціалізується на extraction головного контенту (статті, блоги), видаляючи navigation, sidebar, footer. BS4 — generic HTML parser. Для web scraping trafilatura краще.
- **Content snapshot**: raw HTML зберігається в `metadata["content_snapshot"]` для можливої переобробки з іншими параметрами або LLM. У production це буде зберігатися в `SourceMaterial.content_snapshot` (ORM field).
- **Sync trafilatura в async**: `trafilatura.fetch_url()` — sync HTTP call. Для MVP прийнятно. При потребі — обернути в `asyncio.to_thread()`.
- **Domain extraction**: `urlparse(url).netloc` — простий і надійний спосіб отримати domain.
- **Empty extract ≠ error**: якщо trafilatura не може витягти контент (сторінка з JS-only), повертаємо empty chunks, не ProcessingError. Fetch failure (network error) — це ProcessingError.
