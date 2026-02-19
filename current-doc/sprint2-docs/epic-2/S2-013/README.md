# S2-013: MaterialNode ORM model

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 2h

---

## Мета

ORM модель для вузлів дерева матеріалів

## Що робимо

Створити MaterialNode з self-referential FK, node_fingerprint, relationships

## Як робимо

1. MaterialNode(id, course_id, parent_id→self, title, description, order, node_fingerprint)
2. Relationships: children, materials, parent
3. Cascade delete для children

## Очікуваний результат

MaterialNode ORM працює, можна створювати вкладені вузли

## Як тестуємо

**Автоматизовано:** Unit test: create node, create child, self-ref FK працює

**Human control:** Перевірити в DB що записи створюються з правильними parent_id

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
