# S1-017: MergeStep (SourceDocuments → CourseContext)

## Мета

Об'єднати оброблені `SourceDocument` в єдиний `CourseContext`: сортування за пріоритетом, cross-references slide↔video через `SlideVideoMapEntry`.

## Що робимо

1. **MergeStep** — синхронний клас (pure data transformation, без I/O)
2. **Document ordering** — video → presentation → text → web
3. **Cross-references** — якщо є mappings + presentation + video, збагатити slide chunks video_timecode
4. **Validation** — empty documents → ValueError
5. **Unit-тести** — ~7 тестів

## Контрольні точки

- [ ] Single document → `CourseContext` з 1 doc
- [ ] Multiple documents → правильне сортування
- [ ] Slide-video mappings передаються в `CourseContext`
- [ ] Cross-references: slide chunk отримує `video_timecode` в metadata
- [ ] Empty documents → `ValueError`
- [ ] Default mappings → empty list
- [ ] `make check` проходить

## Залежності

- **Блокується:** S1-011
- **Блокує:** немає
