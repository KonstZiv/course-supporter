# S2-019: Tree API endpoints (nodes)

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 4h

---

## Мета

REST API для управління деревом вузлів

## Що робимо

POST/PATCH/DELETE endpoints для MaterialNode

## Як робимо

1. POST /courses/{id}/nodes — create root node
2. POST /courses/{id}/nodes/{node_id}/children — create child
3. PATCH /courses/{id}/nodes/{node_id} — update title/description/order/parent_id
4. DELETE /courses/{id}/nodes/{node_id} — cascade delete
5. Tenant isolation через CourseRepository.get_by_id()
6. Validation: max depth, cycle detection при move

## Очікуваний результат

Повний CRUD для tree через REST API

## Як тестуємо

**Автоматизовано:** API tests: create tree, move node, delete cascade, tenant isolation, validation errors

**Human control:** Через Postman/curl створити дерево 3 рівні, перемістити вузол, видалити — перевірити response-и

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
