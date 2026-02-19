# S2-034: Extract web scraping — Деталі для виконавця

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 2h

---

## Мета

Web scraping — окрема injectable function

## Контекст

Ця задача є частиною Epic "Heavy Steps Extraction" (2-3 дні).
Загальна ціль epic: Injectable heavy operations, serverless-ready boundary.

## Залежності

**Попередня задача:** [S2-033: Extract vision/slide description](../S2-033/README.md)

**Наступна задача:** [S2-035: Refactor processors as orchestrators](../S2-035/README.md)



---

## Детальний план реалізації

Аналогічно S2-032, але для URL → parsed content

---

## Очікуваний результат

local_scrape_web працює автономно

---

## Тестування

### Автоматизовані тести

Unit test з mock HTTP

### Ручний контроль (Human testing)

Ingestion web URL → результат ідентичний

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
