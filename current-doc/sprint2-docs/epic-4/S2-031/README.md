# S2-031: Heavy step protocols + param/result models

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 2h

---

## Мета

Чіткий contract для кожного heavy step

## Що робимо

Визначити Protocol/Callable types і Pydantic моделі для params/results

## Як робимо

1. TranscribeFunc = Callable[[str, TranscribeParams], Awaitable[Transcript]]
2. DescribeSlidesFunc = Callable[[str, VisionParams], Awaitable[list[SlideDescription]]]
3. ParsePDFFunc, ScrapeWebFunc аналогічно
4. Pydantic models для params і results

## Очікуваний результат

Типізовані contracts для всіх heavy operations

## Як тестуємо

**Автоматизовано:** Type checking (mypy) проходить

**Human control:** Code review: contracts чисті, зрозумілі, не залежать від DB/S3

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
