# S2-026: FingerprintService — course level

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 1h

---

## Мета

Course fingerprint = hash від root nodes

## Що робимо

course_fingerprint(course_id) — top-level hash

## Як робимо

1. Get root nodes (parent_id=None) для course
2. sorted ensure_node_fp для кожного
3. sha256 від joined parts

## Очікуваний результат

Єдиний hash що представляє стан всього дерева курсу

## Як тестуємо

**Автоматизовано:** Unit test: course fp changes when any material changes, stable when nothing changes

**Human control:** Немає

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
