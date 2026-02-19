# S2-048: Subtree readiness check

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 2h

---

## Мета

Знайти stale матеріали (RAW, INTEGRITY_BROKEN) в піддереві

## Що робимо

find_stale_materials(node_id) — рекурсивний пошук

## Як робимо

1. Обійти піддерево від node_id
2. Знайти MaterialEntry де state in (RAW, INTEGRITY_BROKEN)
3. Повернути список з деталями (id, filename, state, node_title)

## Очікуваний результат

Швидка перевірка готовності піддерева до генерації

## Як тестуємо

**Автоматизовано:** Unit tests: all ready, some stale, nested stale, empty tree

**Human control:** Немає

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
