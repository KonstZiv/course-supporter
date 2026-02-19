# S2-049: Conflict detection (subtree overlap)

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 3h

---

## Мета

Визначити чи новий запит на генерацію перетинається з active job

## Що робимо

check_generation_conflict(course_id, target_node_id) → conflicting Job or None

## Як робимо

1. Get active generation jobs for course
2. Для кожного: is_ancestor_or_same(active_node, target_node) в обидва боки
3. _is_ancestor_or_same: traverse parent_id chain

## Очікуваний результат

Conflict detection працює для всіх комбінацій (course↔node, node↔node, siblings)

## Як тестуємо

**Автоматизовано:** Unit tests: all combinations from AR-6 table (course-course, node-child, siblings)

**Human control:** Немає (таблиця сценаріїв покрита unit tests)

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
