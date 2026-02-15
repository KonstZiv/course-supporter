# 📋 S1-015: TextProcessor (MD / DOCX / HTML)

## Мета

Реалізувати обробку текстових документів: Markdown, DOCX, HTML, plain text. Без LLM — чистий парсинг. Розбиття на `HEADING` та `PARAGRAPH` chunks зі збереженням структури (heading levels).

## Контекст

Залежить від S1-011 (schemas + ABC). Не потребує `ModelRouter` — pure parsing. Бібліотеки `python-docx` та `beautifulsoup4` вже в `pyproject.toml`.

---

## Acceptance Criteria

- [ ] `TextProcessor` реалізує `SourceProcessor.process()`
- [ ] Markdown: headings (`#`) → `HEADING` chunks з level, text → `PARAGRAPH` chunks
- [ ] DOCX: heading styles → `HEADING`, paragraphs → `PARAGRAPH`
- [ ] HTML: `<h1>`..`<h6>` → `HEADING`, text → `PARAGRAPH`
- [ ] Plain text (.txt): → single `PARAGRAPH` chunk
- [ ] Непідтримане розширення (.rtf) → `UnsupportedFormatError`
- [ ] Empty file → empty chunks
- [ ] Sequential chunk indexing
- [ ] `router` parameter ignored (no LLM needed)
- [ ] ~9 unit-тестів
- [ ] `make check` проходить

---

## Реалізація

### src/course_supporter/ingestion/text.py

```python
"""Text processor for MD, DOCX, HTML, and plain text files."""

import re
from pathlib import Path

import structlog

from course_supporter.ingestion.base import (
    SourceProcessor,
    UnsupportedFormatError,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
)

logger = structlog.get_logger()

SUPPORTED_EXTENSIONS = {".md", ".markdown", ".docx", ".html", ".htm", ".txt"}


class TextProcessor(SourceProcessor):
    """Process text documents (MD, DOCX, HTML, TXT).

    Extracts headings and paragraphs without LLM.
    The router parameter is accepted but not used.
    """

    async def process(
        self,
        source: "SourceMaterial",
        *,
        router: "ModelRouter | None" = None,
    ) -> SourceDocument:
        if source.source_type != "text":
            raise UnsupportedFormatError(
                f"TextProcessor expects 'text', got '{source.source_type}'"
            )

        path = Path(source.source_url)
        ext = path.suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            raise UnsupportedFormatError(
                f"Unsupported text format: {ext}. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )

        logger.info(
            "text_processing_start",
            source_url=source.source_url,
            format=ext,
        )

        if ext in {".md", ".markdown"}:
            chunks = self._process_markdown(path)
        elif ext == ".docx":
            chunks = self._process_docx(path)
        elif ext in {".html", ".htm"}:
            chunks = self._process_html(path)
        else:  # .txt
            chunks = self._process_plain_text(path)

        logger.info(
            "text_processing_done",
            source_url=source.source_url,
            chunk_count=len(chunks),
        )

        return SourceDocument(
            source_type="text",
            source_url=source.source_url,
            title=source.filename or path.stem,
            chunks=chunks,
        )

    def _process_markdown(self, path: Path) -> list[ContentChunk]:
        """Parse Markdown file into heading/paragraph chunks.

        Headings: lines starting with # (1-6 levels).
        Paragraphs: non-empty text between headings.
        """
        content = path.read_text(encoding="utf-8")
        return self._parse_markdown_text(content)

    @staticmethod
    def _parse_markdown_text(content: str) -> list[ContentChunk]:
        """Parse markdown text content into chunks."""
        chunks: list[ContentChunk] = []
        heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

        idx = 0
        last_end = 0

        for match in heading_pattern.finditer(content):
            # Text before this heading → paragraph
            before = content[last_end:match.start()].strip()
            if before:
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.PARAGRAPH,
                        text=before,
                        index=idx,
                    )
                )
                idx += 1

            # Heading itself
            level = len(match.group(1))
            heading_text = match.group(2).strip()
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.HEADING,
                    text=heading_text,
                    index=idx,
                    metadata={"level": level},
                )
            )
            idx += 1
            last_end = match.end()

        # Remaining text after last heading
        remaining = content[last_end:].strip()
        if remaining:
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.PARAGRAPH,
                    text=remaining,
                    index=idx,
                )
            )

        return chunks

    @staticmethod
    def _process_docx(path: Path) -> list[ContentChunk]:
        """Parse DOCX file into heading/paragraph chunks.

        Detects heading styles (Heading 1..6) and maps to levels.
        """
        from docx import Document  # type: ignore[import-untyped]

        doc = Document(str(path))
        chunks: list[ContentChunk] = []
        idx = 0

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            style_name = para.style.name if para.style else ""

            if style_name.startswith("Heading"):
                # Extract level from "Heading 1", "Heading 2", etc.
                try:
                    level = int(style_name.split()[-1])
                except (ValueError, IndexError):
                    level = 1

                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.HEADING,
                        text=text,
                        index=idx,
                        metadata={"level": level},
                    )
                )
            else:
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.PARAGRAPH,
                        text=text,
                        index=idx,
                    )
                )
            idx += 1

        return chunks

    @staticmethod
    def _process_html(path: Path) -> list[ContentChunk]:
        """Parse HTML file into heading/paragraph chunks.

        Uses BeautifulSoup to extract <h1>..<h6> and <p> elements.
        """
        from bs4 import BeautifulSoup

        html = path.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        chunks: list[ContentChunk] = []
        idx = 0

        for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p"]):
            text = element.get_text(strip=True)
            if not text:
                continue

            if element.name.startswith("h"):
                level = int(element.name[1])
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.HEADING,
                        text=text,
                        index=idx,
                        metadata={"level": level},
                    )
                )
            else:
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.PARAGRAPH,
                        text=text,
                        index=idx,
                    )
                )
            idx += 1

        return chunks

    @staticmethod
    def _process_plain_text(path: Path) -> list[ContentChunk]:
        """Read plain text file as a single paragraph chunk."""
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return []
        return [
            ContentChunk(
                chunk_type=ChunkType.PARAGRAPH,
                text=content,
                index=0,
            )
        ]
```

---

## Тести

### tests/unit/test_ingestion/test_text.py

```python
"""Tests for TextProcessor."""

from unittest.mock import MagicMock, patch

import pytest

from course_supporter.ingestion.base import UnsupportedFormatError
from course_supporter.ingestion.text import TextProcessor
from course_supporter.models.source import ChunkType, SourceDocument


def _make_source(
    source_type: str = "text",
    url: str = "file:///doc.md",
    filename: str = "doc.md",
) -> MagicMock:
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
            with patch("pathlib.Path.suffix", new_callable=lambda: property(lambda s: ".md")):
                proc = TextProcessor()
                doc = await proc.process(_make_source())

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
            await proc.process(
                _make_source(url="file:///f.rtf", filename="f.rtf")
            )

    async def test_chunk_ordering(self) -> None:
        """Chunks indexed sequentially."""
        md_content = "# H1\n\nPara\n\n## H2\n\nPara2"

        with patch("pathlib.Path.read_text", return_value=md_content):
            proc = TextProcessor()
            doc = await proc.process(_make_source())

        indices = [c.index for c in doc.chunks]
        assert indices == list(range(len(indices)))
```

---

## Структура файлів

```
src/course_supporter/ingestion/
├── text.py                  # TextProcessor

tests/unit/test_ingestion/
└── test_text.py             # ~9 tests
```

---

## Кроки виконання

1. Переконатися, що S1-011 завершено
2. Реалізувати `TextProcessor` в `ingestion/text.py`
3. Створити `tests/unit/test_ingestion/test_text.py`
4. `make check`

---

## Примітки

- **Без LLM**: TextProcessor — pure parsing, не потребує `ModelRouter`. Параметр `router` приймається для сумісності з `SourceProcessor` ABC, але ігнорується.
- **Markdown parsing**: regex-based, не повний CommonMark parser. Для MVP достатньо — headings `#` та text між ними. Якщо потрібен складніший парсинг — можна замінити на `markdown-it-py`.
- **HTML via BeautifulSoup**: `html.parser` — built-in, не потребує `lxml`. Витягуємо тільки `h1`..`h6` та `p` — ігноруємо `div`, `span`, `li` тощо. При потребі розширити.
- **Encoding**: завжди `utf-8`. Edge cases з іншими кодуваннями — за межами MVP.
- **Empty paragraphs skipped**: порожні рядки та whitespace-only не створюють chunks.
