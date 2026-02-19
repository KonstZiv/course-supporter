# S2-023: Tree + MaterialEntry unit tests

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 5h

---

## Мета

Повне тестове покриття для tree і entry operations

## Що робимо

Comprehensive unit tests для всього Epic 2

## Як робимо

1. MaterialNode: CRUD, move з cycle detection, cascade delete, deep nesting (5+ рівнів)
2. MaterialEntry: state transitions, pending lifecycle, hash invalidation
3. API: full flow (create course → tree → materials → verify)
4. Edge cases: empty tree, single node, very deep tree

## Очікуваний результат

Всі тести зелені, coverage > 90% для нових модулів

## Як тестуємо

**Автоматизовано:** pytest з coverage report

**Human control:** Review test cases — чи покриті edge cases, чи тести читабельні

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
