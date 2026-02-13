# 📋 S1-017: MergeStep (SourceDocuments → CourseContext)

## Мета

Об'єднати оброблені `SourceDocument` об'єкти в єдиний `CourseContext`: сортування документів за пріоритетом (video → presentation → text → web), cross-references між slide та video через `SlideVideoMapEntry`.

## Контекст

Залежить від S1-011 (schemas: SourceDocument, CourseContext, SlideVideoMapEntry). Не потребує `ModelRouter` — pure data transformation, синхронний код (не async). `MergeStep` — фінальний крок перед передачею в ArchitectAgent.

---

## Acceptance Criteria

- [ ] `MergeStep.merge()` повертає `CourseContext`
- [ ] Documents сортовані за пріоритетом: video → presentation → text → web
- [ ] Slide-video mappings передані в `CourseContext`
- [ ] Cross-references: presentation SLIDE_TEXT chunks збагачені `video_timecode` в metadata
- [ ] Empty documents → `ValueError`
- [ ] Default mappings → empty list
- [ ] Синхронний метод (не async)
- [ ] ~7 unit-тестів
- [ ] `make check` проходить

---

## Реалізація

### src/course_supporter/ingestion/merge.py

```python
"""Merge step for combining processed source documents into CourseContext."""

import structlog

from course_supporter.models.course import CourseContext, SlideVideoMapEntry
from course_supporter.models.source import ChunkType, ContentChunk, SourceDocument

logger = structlog.get_logger()

# Priority order for document types (lower index = higher priority)
SOURCE_TYPE_PRIORITY: dict[str, int] = {
    "video": 0,
    "presentation": 1,
    "text": 2,
    "web": 3,
}


class MergeStep:
    """Merge multiple SourceDocuments into a unified CourseContext.

    Responsibilities:
    1. Sort documents by source type priority (video first, web last)
    2. Cross-reference slide↔video via SlideVideoMapEntry mappings
    3. Package everything into CourseContext for ArchitectAgent

    This is a synchronous, pure data transformation — no I/O, no LLM.
    """

    def merge(
        self,
        documents: list[SourceDocument],
        mappings: list[SlideVideoMapEntry] | None = None,
    ) -> CourseContext:
        """Merge source documents and optional mappings into CourseContext.

        Args:
            documents: List of processed SourceDocuments.
            mappings: Optional slide↔video mappings for cross-referencing.

        Returns:
            CourseContext with sorted documents and mappings.

        Raises:
            ValueError: If documents list is empty.
        """
        if not documents:
            raise ValueError("Cannot merge empty documents list")

        resolved_mappings = mappings or []

        # Sort documents by source_type priority
        sorted_docs = sorted(
            documents,
            key=lambda d: SOURCE_TYPE_PRIORITY.get(d.source_type, 99),
        )

        # Cross-reference slides with video timecodes
        if resolved_mappings:
            sorted_docs = self._apply_cross_references(
                sorted_docs, resolved_mappings
            )

        logger.info(
            "merge_complete",
            document_count=len(sorted_docs),
            mapping_count=len(resolved_mappings),
            source_types=[d.source_type for d in sorted_docs],
        )

        return CourseContext(
            documents=sorted_docs,
            slide_video_mappings=resolved_mappings,
        )

    @staticmethod
    def _apply_cross_references(
        documents: list[SourceDocument],
        mappings: list[SlideVideoMapEntry],
    ) -> list[SourceDocument]:
        """Enrich presentation slide chunks with video timecode references.

        For each SLIDE_TEXT chunk in presentation documents, if a mapping
        exists for that slide_number, add video_timecode to chunk metadata.

        Args:
            documents: Sorted list of SourceDocuments.
            mappings: Slide-to-video timecode mappings.

        Returns:
            Documents with enriched presentation chunks.
        """
        # Build lookup: slide_number → video_timecode
        timecode_map: dict[int, str] = {
            m.slide_number: m.video_timecode for m in mappings
        }

        if not timecode_map:
            return documents

        enriched: list[SourceDocument] = []

        for doc in documents:
            if doc.source_type != "presentation":
                enriched.append(doc)
                continue

            new_chunks: list[ContentChunk] = []
            for chunk in doc.chunks:
                if chunk.chunk_type == ChunkType.SLIDE_TEXT:
                    slide_num = chunk.metadata.get("slide_number")
                    if slide_num is not None and slide_num in timecode_map:
                        # Create new chunk with video_timecode in metadata
                        updated_metadata = {
                            **chunk.metadata,
                            "video_timecode": timecode_map[slide_num],
                        }
                        new_chunks.append(
                            chunk.model_copy(
                                update={"metadata": updated_metadata}
                            )
                        )
                        continue
                new_chunks.append(chunk)

            enriched.append(
                doc.model_copy(update={"chunks": new_chunks})
            )

        return enriched
```

---

## Тести

### tests/unit/test_ingestion/test_merge.py

```python
"""Tests for MergeStep."""

import pytest

from course_supporter.ingestion.merge import MergeStep
from course_supporter.models.course import CourseContext, SlideVideoMapEntry
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
)


def _make_doc(source_type: str, url: str = "file:///test") -> SourceDocument:
    """Create a minimal SourceDocument."""
    return SourceDocument(source_type=source_type, source_url=url)


def _make_presentation_doc() -> SourceDocument:
    """Create a presentation SourceDocument with slide chunks."""
    return SourceDocument(
        source_type="presentation",
        source_url="file:///slides.pdf",
        chunks=[
            ContentChunk(
                chunk_type=ChunkType.SLIDE_TEXT,
                text="Slide 1 content",
                index=1,
                metadata={"slide_number": 1},
            ),
            ContentChunk(
                chunk_type=ChunkType.SLIDE_TEXT,
                text="Slide 2 content",
                index=2,
                metadata={"slide_number": 2},
            ),
        ],
    )


class TestMergeStep:
    def test_merge_single_document(self) -> None:
        """One doc → CourseContext with 1 doc."""
        step = MergeStep()
        ctx = step.merge([_make_doc("text")])

        assert isinstance(ctx, CourseContext)
        assert len(ctx.documents) == 1
        assert ctx.slide_video_mappings == []

    def test_merge_multiple_documents(self) -> None:
        """Video + text → CourseContext with 2 docs."""
        step = MergeStep()
        ctx = step.merge([
            _make_doc("text"),
            _make_doc("video"),
        ])

        assert len(ctx.documents) == 2

    def test_merge_with_mappings(self) -> None:
        """Documents + slide-video mappings in CourseContext."""
        step = MergeStep()
        mappings = [
            SlideVideoMapEntry(slide_number=1, video_timecode="00:05:30"),
        ]
        ctx = step.merge([_make_doc("video")], mappings=mappings)

        assert len(ctx.slide_video_mappings) == 1
        assert ctx.slide_video_mappings[0].video_timecode == "00:05:30"

    def test_merge_empty_documents(self) -> None:
        """Empty list → raise ValueError."""
        step = MergeStep()
        with pytest.raises(ValueError, match="Cannot merge empty"):
            step.merge([])

    def test_merge_document_ordering(self) -> None:
        """Video first, web last regardless of input order."""
        step = MergeStep()
        ctx = step.merge([
            _make_doc("web"),
            _make_doc("text"),
            _make_doc("video"),
            _make_doc("presentation"),
        ])

        types = [d.source_type for d in ctx.documents]
        assert types == ["video", "presentation", "text", "web"]

    def test_merge_no_mappings_default(self) -> None:
        """Mappings defaults to empty list."""
        step = MergeStep()
        ctx = step.merge([_make_doc("text")])

        assert ctx.slide_video_mappings == []

    def test_merge_cross_references(self) -> None:
        """Presentation chunk gets video_timecode in metadata."""
        step = MergeStep()
        mappings = [
            SlideVideoMapEntry(slide_number=1, video_timecode="00:10:00"),
            SlideVideoMapEntry(slide_number=2, video_timecode="00:25:00"),
        ]
        pres_doc = _make_presentation_doc()

        ctx = step.merge(
            [_make_doc("video"), pres_doc],
            mappings=mappings,
        )

        # Find presentation doc in result
        pres = [d for d in ctx.documents if d.source_type == "presentation"][0]
        slide1 = pres.chunks[0]
        slide2 = pres.chunks[1]

        assert slide1.metadata["video_timecode"] == "00:10:00"
        assert slide2.metadata["video_timecode"] == "00:25:00"
```

---

## Структура файлів

```
src/course_supporter/ingestion/
├── merge.py                 # MergeStep

tests/unit/test_ingestion/
└── test_merge.py            # ~7 tests
```

---

## Кроки виконання

1. Переконатися, що S1-011 завершено
2. Реалізувати `MergeStep` в `ingestion/merge.py`
3. Створити `tests/unit/test_ingestion/test_merge.py`
4. `make check`

---

## Примітки

- **Синхронний**: `MergeStep.merge()` — звичайний sync метод, не `async`. Pure data transformation, без I/O.
- **Immutability**: `model_copy(update={...})` — Pydantic v2 спосіб створити копію з оновленими полями. Оригінальні документи не мутуються.
- **SOURCE_TYPE_PRIORITY**: video (0) → presentation (1) → text (2) → web (3). Unknown types отримують priority 99 (в кінець).
- **Cross-references**: тільки SLIDE_TEXT chunks з `slide_number` у metadata. SLIDE_DESCRIPTION chunks ігноруються (вони описують зображення, не мають slide_number для mapping).
- **SlideVideoMapEntry**: mirrors ORM `SlideVideoMapping` — `video_timecode` це String(20), наприклад "01:23:45".
