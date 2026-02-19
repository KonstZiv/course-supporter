# S2-053: Structure generation API

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 4h

---

## Мета

Повний API для trigger і перегляду результатів генерації

## Що робимо

POST trigger (200/202/409/422), GET status, GET result

## Як робимо

1. POST /courses/{id}/structure/generate і /nodes/{node_id}/structure/generate
2. Response codes: 200 (existing), 202 (new), 409 (conflict), 422 (no ready materials)
3. GET /structure — latest snapshot
4. GET /structure/jobs — generation jobs list

## Очікуваний результат

Повний API для generation workflow

## Як тестуємо

**Автоматизовано:** API tests: all 4 response codes, GET status, GET result, tenant isolation

**Human control:** Повний flow через API: create → upload → generate → poll → get result

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
