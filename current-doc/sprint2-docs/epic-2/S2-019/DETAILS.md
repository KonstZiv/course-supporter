# S2-019: Tree API endpoints (nodes) — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 4h

---

## Мета

REST API для управління деревом вузлів

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Попередня задача:** [S2-018: Alembic migration: new tables + data migration](../S2-018/README.md)

**Наступна задача:** [S2-020: Materials endpoint refactor](../S2-020/README.md)



---

## Детальний план реалізації

1. POST /courses/{id}/nodes — create root node
2. POST /courses/{id}/nodes/{node_id}/children — create child
3. PATCH /courses/{id}/nodes/{node_id} — update title/description/order/parent_id
4. DELETE /courses/{id}/nodes/{node_id} — cascade delete
5. Tenant isolation через CourseRepository.get_by_id()
6. Validation: max depth, cycle detection при move

---

## Очікуваний результат

Повний CRUD для tree через REST API

---

## Тестування

### Автоматизовані тести

API tests: create tree, move node, delete cascade, tenant isolation, validation errors

### Ручний контроль (Human testing)

Через Postman/curl створити дерево 3 рівні, перемістити вузол, видалити — перевірити response-и

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
