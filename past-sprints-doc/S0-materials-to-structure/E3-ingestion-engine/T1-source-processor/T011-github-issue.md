# S1-011: SourceProcessor Interface + Pydantic Schemas

## Мета

Визначити базовий контракт для обробки матеріалів (`SourceProcessor` ABC) та Pydantic-моделі для представлення оброблених даних (`ContentChunk`, `SourceDocument`, `CourseContext`).

## Що робимо

1. **Pydantic schemas** — `ChunkType` (StrEnum), `ContentChunk`, `SourceDocument` в `models/source.py`
2. **CourseContext** — `SlideVideoMapEntry`, `CourseContext` в `models/course.py`
3. **SourceProcessor ABC** — абстрактний `process()` з сигнатурою `(source, *, router=None) -> SourceDocument`
4. **Custom exceptions** — `ProcessingError`, `UnsupportedFormatError` в `ingestion/base.py`
5. **Exports** — оновити `__init__.py` для `models/` та `ingestion/`
6. **Unit-тести** — ~8 тестів на schemas + ABC

## Контрольні точки

- [ ] `ChunkType` StrEnum містить 7 типів (transcript, slide_text, slide_description, paragraph, heading, web_content, metadata)
- [ ] `ContentChunk` має defaults (empty dict metadata, index=0)
- [ ] `SourceDocument` має auto `processed_at`
- [ ] `CourseContext` об'єднує documents + slide_video_mappings
- [ ] `SourceProcessor` — не можна інстанціювати
- [ ] `ProcessingError`, `UnsupportedFormatError` визначені
- [ ] `make check` проходить

## Залежності

- **Блокується:** немає
- **Блокує:** S1-012, S1-013, S1-014, S1-015, S1-016, S1-017
