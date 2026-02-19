# S2-020: Materials endpoint refactor — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 3h

---

## Мета

Матеріали завантажуються через node (не напряму в course)

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Попередня задача:** [S2-019: Tree API endpoints (nodes)](../S2-019/README.md)

**Наступна задача:** [S2-021: Course detail response — tree structure](../S2-021/README.md)



---

## Детальний план реалізації

1. POST /courses/{id}/nodes/{node_id}/materials — додати матеріал до вузла
2. DELETE /courses/{id}/materials/{material_id} — видалити
3. POST /courses/{id}/materials/{material_id}/retry — повторити ingestion
4. При створенні: auto-enqueue ingestion job (S2-008)

---

## Очікуваний результат

Матеріали завантажуються в конкретний вузол, auto-ingestion працює

---

## Тестування

### Автоматизовані тести

API tests: upload to node, delete, retry, wrong node → 404, wrong tenant → 404

### Ручний контроль (Human testing)

Завантажити файл в конкретний вузол → GET course → перевірити що матеріал в правильному вузлі

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
