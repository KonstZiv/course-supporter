# S2-022: List courses endpoint — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 1h

---

## Мета

GET /courses повертає список курсів з pagination

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Попередня задача:** [S2-021: Course detail response — tree structure](../S2-021/README.md)

**Наступна задача:** [S2-023: Tree + MaterialEntry unit tests](../S2-023/README.md)



---

## Детальний план реалізації

1. GET /api/v1/courses?offset=0&limit=20
2. Response: items[], total, offset, limit
3. Tenant-scoped (тільки свої курси)

---

## Очікуваний результат

GET /courses повертає список з pagination

---

## Тестування

### Автоматизовані тести

API test: create 5 courses, pagination (limit=2), tenant isolation

### Ручний контроль (Human testing)

GET /courses — перевірити що повертаються тільки свої курси

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
