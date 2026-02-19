# S2-016: MaterialNode repository

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 4h

---

## Мета

CRUD + tree operations для MaterialNode

## Що робимо

MaterialNodeRepository з create, get, update, delete, reorder, move, recursive fetch

## Як робимо

1. create(course_id, parent_id, title) з order auto-increment
2. get_tree(course_id) — recursive eager load
3. move(node_id, new_parent_id) з валідацією циклів
4. reorder(node_id, new_order) з shift siblings
5. delete з cascade

## Очікуваний результат

Повний CRUD для tree nodes з валідацією

## Як тестуємо

**Автоматизовано:** Unit tests: create, move (з cycle detection), reorder, cascade delete, get_tree depth 5

**Human control:** Через DB client створити дерево 4 рівні → get_tree → перевірити повноту

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
