# 📋 S1-011: SourceProcessor Interface + Pydantic Schemas

## Мета

Визначити базовий контракт для обробки курсових матеріалів та Pydantic-моделі для представлення оброблених даних. `SourceProcessor` ABC задає сигнатуру `process(source, *, router=None) -> SourceDocument`. Pydantic-моделі (`ContentChunk`, `SourceDocument`, `CourseContext`) описують уніфікований формат виходу всіх процесорів.

## Контекст

Перша задача Epic 3 (Ingestion Engine). Блокує всі інші задачі Epic 3 (крім S1-018). Epic 1–2 завершені (84 тести). Файли-заглушки (`models/source.py`, `models/course.py`, `ingestion/base.py`) вже існують — потрібно замінити TODO на реальний код.

---

## Acceptance Criteria

- [ ] `ChunkType` StrEnum з 7 значеннями
- [ ] `ContentChunk` Pydantic model з defaults (empty dict metadata, index=0)
- [ ] `SourceDocument` Pydantic model з auto `processed_at`
- [ ] `SlideVideoMapEntry` — Pydantic mirror для ORM `SlideVideoMapping`
- [ ] `CourseContext` — об'єднує documents + slide_video_mappings
- [ ] `SourceProcessor` ABC — `process()` abstractmethod, не можна інстанціювати
- [ ] `ProcessingError` та `UnsupportedFormatError` exceptions
- [ ] Exports в `__init__.py` для обох пакетів
- [ ] ~8 unit-тестів, всі зелені
- [ ] `make check` проходить

---

## Pydantic-схеми

### src/course_supporter/models/source.py

```python
"""Source material schemas for ingestion pipeline."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ChunkType(StrEnum):
    """Types of content chunks produced by processors."""

    TRANSCRIPT = "transcript"
    SLIDE_TEXT = "slide_text"
    SLIDE_DESCRIPTION = "slide_description"
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    WEB_CONTENT = "web_content"
    METADATA = "metadata"


class ContentChunk(BaseModel):
    """Single chunk of extracted content.

    Each processor produces a list of these. The chunk_type identifies
    the source (transcript, slide text, etc.) and metadata carries
    type-specific details (timecodes, slide numbers, heading levels).
    """

    chunk_type: ChunkType
    text: str
    index: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceDocument(BaseModel):
    """Unified output of any SourceProcessor.

    Contains all extracted content from a single source material
    (one video, one PDF, etc.) as a list of ContentChunks.
    """

    source_type: str
    source_url: str
    title: str = ""
    chunks: list[ContentChunk] = Field(default_factory=list)
    processed_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

**metadata examples:**
- transcript chunk: `{"start_sec": 0.0, "end_sec": 30.0}`
- slide_text chunk: `{"slide_number": 1, "has_diagram": True}`
- heading chunk: `{"level": 2}`
- SourceDocument video: `{"duration_sec": 3600, "strategy": "gemini"}`
- SourceDocument presentation: `{"page_count": 25, "format": "pdf"}`
- SourceDocument web: `{"fetched_at": "...", "domain": "example.com"}`

### src/course_supporter/models/course.py

```python
"""Course structure schemas."""

from datetime import datetime

from pydantic import BaseModel, Field

from course_supporter.models.source import SourceDocument


class SlideVideoMapEntry(BaseModel):
    """Pydantic mirror of ORM SlideVideoMapping.

    Maps slide_number to video_timecode (e.g., "01:23:45").
    Matches ORM: String(20) for video_timecode.
    """

    slide_number: int
    video_timecode: str


class CourseContext(BaseModel):
    """Unified context for course structuring.

    Combines all processed source documents and optional
    slide-video mappings into a single object for the
    ArchitectAgent.
    """

    documents: list[SourceDocument]
    slide_video_mappings: list[SlideVideoMapEntry] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
```

**Примітка щодо ORM:** `SlideVideoMapping` ORM має `video_timecode: Mapped[str] = mapped_column(String(20))` — це рядок вигляду "01:23:45", НЕ start/end floats. Pydantic model це дзеркалює.

### src/course_supporter/models/__init__.py

```python
"""Pydantic schemas for course-supporter domain models."""

from course_supporter.models.course import CourseContext, SlideVideoMapEntry
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
)

__all__ = [
    "ChunkType",
    "ContentChunk",
    "CourseContext",
    "SlideVideoMapEntry",
    "SourceDocument",
]
```

---

## SourceProcessor ABC + Exceptions

### src/course_supporter/ingestion/base.py

```python
"""SourceProcessor abstract base class and custom exceptions."""

import abc

from course_supporter.models.source import SourceDocument


class ProcessingError(Exception):
    """Raised when a processor fails to process source material."""


class UnsupportedFormatError(ProcessingError):
    """Raised when source material format is not supported by processor."""


class SourceProcessor(abc.ABC):
    """Abstract base class for all source material processors.

    Each processor transforms a SourceMaterial into a SourceDocument
    containing extracted ContentChunks.
    """

    @abc.abstractmethod
    async def process(
        self,
        source: "SourceMaterial",
        *,
        router: "ModelRouter | None" = None,
    ) -> SourceDocument:
        """Process source material and return structured document.

        Args:
            source: The source material to process.
            router: Optional ModelRouter for LLM-powered processing
                    (vision analysis, transcription via Gemini, etc.).

        Returns:
            SourceDocument with extracted content chunks.

        Raises:
            ProcessingError: If processing fails.
            UnsupportedFormatError: If source format is not supported.
        """
        ...
```

**Примітки щодо type hints:**
- `SourceMaterial` та `ModelRouter` — string forward references, щоб уникнути circular imports
- На практиці `source` буде `course_supporter.storage.orm.SourceMaterial` (ORM model)
- `router` буде `course_supporter.llm.router.ModelRouter`

### src/course_supporter/ingestion/__init__.py

```python
"""Ingestion pipeline for processing course materials."""

from course_supporter.ingestion.base import (
    ProcessingError,
    SourceProcessor,
    UnsupportedFormatError,
)

__all__ = [
    "ProcessingError",
    "SourceProcessor",
    "UnsupportedFormatError",
]
```

---

## Тести

### tests/unit/test_ingestion/__init__.py

Порожній файл.

### tests/unit/test_ingestion/test_schemas.py

```python
"""Tests for ingestion pipeline schemas and interfaces."""

from datetime import datetime

import pytest

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
from course_supporter.models.course import CourseContext, SlideVideoMapEntry


class TestChunkType:
    def test_chunk_type_values(self) -> None:
        """All expected chunk types exist with correct string values."""
        assert ChunkType.TRANSCRIPT == "transcript"
        assert ChunkType.SLIDE_TEXT == "slide_text"
        assert ChunkType.SLIDE_DESCRIPTION == "slide_description"
        assert ChunkType.PARAGRAPH == "paragraph"
        assert ChunkType.HEADING == "heading"
        assert ChunkType.WEB_CONTENT == "web_content"
        assert ChunkType.METADATA == "metadata"


class TestContentChunk:
    def test_content_chunk_default_metadata(self) -> None:
        """ContentChunk metadata defaults to empty dict."""
        chunk = ContentChunk(chunk_type=ChunkType.PARAGRAPH, text="hello")
        assert chunk.metadata == {}
        assert chunk.index == 0

    def test_content_chunk_with_timecodes(self) -> None:
        """Transcript chunk carries start/end timecodes in metadata."""
        chunk = ContentChunk(
            chunk_type=ChunkType.TRANSCRIPT,
            text="Hello world",
            index=0,
            metadata={"start_sec": 0.0, "end_sec": 30.0},
        )
        assert chunk.metadata["start_sec"] == 0.0
        assert chunk.metadata["end_sec"] == 30.0


class TestSourceDocument:
    def test_source_document_defaults(self) -> None:
        """SourceDocument has empty chunks and auto processed_at."""
        doc = SourceDocument(source_type="text", source_url="file:///test.md")
        assert doc.chunks == []
        assert doc.title == ""
        assert isinstance(doc.processed_at, datetime)
        assert doc.metadata == {}

    def test_source_document_with_chunks(self) -> None:
        """SourceDocument holds multiple content chunks."""
        chunks = [
            ContentChunk(chunk_type=ChunkType.HEADING, text="Title", index=0),
            ContentChunk(chunk_type=ChunkType.PARAGRAPH, text="Body", index=1),
        ]
        doc = SourceDocument(
            source_type="text",
            source_url="file:///test.md",
            title="My Doc",
            chunks=chunks,
        )
        assert len(doc.chunks) == 2
        assert doc.chunks[0].chunk_type == ChunkType.HEADING


class TestCourseContext:
    def test_course_context_empty(self) -> None:
        """CourseContext with no documents."""
        ctx = CourseContext(documents=[])
        assert ctx.documents == []
        assert ctx.slide_video_mappings == []
        assert isinstance(ctx.created_at, datetime)

    def test_course_context_with_mappings(self) -> None:
        """CourseContext with documents and slide-video mappings."""
        doc = SourceDocument(source_type="video", source_url="file:///v.mp4")
        mapping = SlideVideoMapEntry(slide_number=1, video_timecode="00:05:30")
        ctx = CourseContext(
            documents=[doc],
            slide_video_mappings=[mapping],
        )
        assert len(ctx.documents) == 1
        assert ctx.slide_video_mappings[0].slide_number == 1
        assert ctx.slide_video_mappings[0].video_timecode == "00:05:30"


class TestSourceProcessor:
    def test_source_processor_is_abstract(self) -> None:
        """SourceProcessor cannot be instantiated directly."""
        with pytest.raises(TypeError):
            SourceProcessor()  # type: ignore[abstract]

    def test_processing_error_hierarchy(self) -> None:
        """UnsupportedFormatError is a subclass of ProcessingError."""
        assert issubclass(UnsupportedFormatError, ProcessingError)
        assert issubclass(ProcessingError, Exception)
```

---

## Структура файлів

```
src/course_supporter/
├── models/
│   ├── __init__.py          # exports: ChunkType, ContentChunk, SourceDocument, CourseContext, SlideVideoMapEntry
│   ├── source.py            # ChunkType, ContentChunk, SourceDocument
│   └── course.py            # SlideVideoMapEntry, CourseContext
├── ingestion/
│   ├── __init__.py          # exports: SourceProcessor, ProcessingError, UnsupportedFormatError
│   └── base.py              # SourceProcessor ABC, ProcessingError, UnsupportedFormatError

tests/unit/test_ingestion/
├── __init__.py
└── test_schemas.py          # ~8 tests
```

---

## Кроки виконання

1. Замінити `models/source.py` — `ChunkType`, `ContentChunk`, `SourceDocument`
2. Замінити `models/course.py` — `SlideVideoMapEntry`, `CourseContext`
3. Оновити `models/__init__.py` — exports
4. Замінити `ingestion/base.py` — `SourceProcessor` ABC, exceptions
5. Оновити `ingestion/__init__.py` — exports
6. Створити `tests/unit/test_ingestion/__init__.py`
7. Створити `tests/unit/test_ingestion/test_schemas.py`
8. `make check`

---

## Примітки

- **Forward references**: `SourceMaterial` та `ModelRouter` — string refs у сигнатурі `process()`, щоб уникнути circular imports. Типи доступні runtime через `TYPE_CHECKING` або просто як string annotations.
- **ChunkType як StrEnum**: Python 3.11+ StrEnum — значення chunk_type серіалізуються як звичайні рядки в JSON, що спрощує роботу з API та логуванням.
- **processed_at**: `datetime.now` (без UTC) — відповідає стилю `LLMResponse.finished_at` з Epic 2. При потребі перехід на `datetime.now(UTC)` — окремий рефакторинг.
- **Pydantic vs ORM**: `SourceDocument` — Pydantic model для pipeline data flow; `SourceMaterial` — SQLAlchemy ORM для persistence. Вони мають різне призначення і не мають наслідувати одна одну.
