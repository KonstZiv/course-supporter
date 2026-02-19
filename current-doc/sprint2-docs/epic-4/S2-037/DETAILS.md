# S2-037: Heavy steps unit tests — Деталі для виконавця

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 3h

---

## Мета

Повне тестове покриття для нової архітектури processors

## Контекст

Ця задача є частиною Epic "Heavy Steps Extraction" (2-3 дні).
Загальна ціль epic: Injectable heavy operations, serverless-ready boundary.

## Залежності

**Попередня задача:** [S2-036: Factory for heavy steps](../S2-036/README.md)



---

## Детальний план реалізації

1. Processor + mock heavy step → correct SourceDocument
2. Processor handles heavy step failure gracefully
3. Factory returns correct implementations
4. Type checking (mypy strict)

---

## Очікуваний результат

Всі тести зелені, mypy strict проходить

---

## Тестування

### Автоматизовані тести

pytest + mypy

### Ручний контроль (Human testing)

Немає

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
