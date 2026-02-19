# S2-035: Refactor processors as orchestrators — Деталі для виконавця

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 4h

---

## Мета

Processors приймають heavy steps через DI, не знають implementation

## Контекст

Ця задача є частиною Epic "Heavy Steps Extraction" (2-3 дні).
Загальна ціль epic: Injectable heavy operations, serverless-ready boundary.

## Залежності

**Попередня задача:** [S2-034: Extract web scraping](../S2-034/README.md)

**Наступна задача:** [S2-036: Factory for heavy steps](../S2-036/README.md)



---

## Детальний план реалізації

1. Processor.__init__(heavy_step_func) — injectable
2. Processor.process(): prepare input → call heavy step → package SourceDocument
3. Processor не імпортує whisper/vision напряму

---

## Очікуваний результат

Processors — тонкі оркестратори, heavy logic injectable

---

## Тестування

### Автоматизовані тести

Unit tests: processor з mock heavy step → правильний SourceDocument

### Ручний контроль (Human testing)

Code review: processors не мають прямих imports heavy libraries

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
