# S2-047: CourseStructureSnapshot ORM + repository

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 3h

---

## Мета

Snapshot зберігає результат генерації з прив'язкою до fingerprint і scope

## Що робимо

ORM model і repository для CourseStructureSnapshot

## Як робимо

1. Model: course_id, node_id (nullable), node_fingerprint, mode, structure (JSONB), LLM metadata
2. Unique constraint: (course_id, node_id, fingerprint, mode)
3. Repository: create, find_by_identity, get_latest

## Очікуваний результат

Snapshots зберігаються з прив'язкою до fingerprint для idempotency

## Як тестуємо

**Автоматизовано:** Unit tests: CRUD, unique constraint violation, find by identity

**Human control:** Немає

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
