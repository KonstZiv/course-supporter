# S2-031: Heavy step protocols + param/result models — Деталі для виконавця

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 2h

---

## Мета

Чіткий contract для кожного heavy step

## Контекст

Ця задача є частиною Epic "Heavy Steps Extraction" (2-3 дні).
Загальна ціль epic: Injectable heavy operations, serverless-ready boundary.

## Залежності

**Наступна задача:** [S2-032: Extract whisper transcription](../S2-032/README.md)



---

## Детальний план реалізації

1. TranscribeFunc = Callable[[str, TranscribeParams], Awaitable[Transcript]]
2. DescribeSlidesFunc = Callable[[str, VisionParams], Awaitable[list[SlideDescription]]]
3. ParsePDFFunc, ScrapeWebFunc аналогічно
4. Pydantic models для params і results

---

## Очікуваний результат

Типізовані contracts для всіх heavy operations

---

## Тестування

### Автоматизовані тести

Type checking (mypy) проходить

### Ручний контроль (Human testing)

Code review: contracts чисті, зрозумілі, не залежать від DB/S3

---

## Checklist перед PR

- [ ] Реалізація відповідає архітектурним рішенням Sprint 2 (AR-*)
- [ ] Код проходить `make check` (ruff + mypy + pytest)
- [ ] Unit tests написані і покривають основні сценарії
- [ ] Edge cases покриті (error handling, boundary values)
- [ ] Error messages зрозумілі і містять hints для користувача
- [ ] Human control points пройдені
- [ ] Документація оновлена якщо потрібно (ERD, API docs, sprint progress)
- [ ] Перевірено чи зміни впливають на наступні задачі — якщо так, оновити їх docs

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
