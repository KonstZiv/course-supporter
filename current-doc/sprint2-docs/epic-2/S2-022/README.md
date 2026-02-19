# S2-022: List courses endpoint

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 1h

---

## Мета

GET /courses повертає список курсів з pagination

## Що робимо

Додати GET /api/v1/courses з offset/limit pagination

## Як робимо

1. GET /api/v1/courses?offset=0&limit=20
2. Response: items[], total, offset, limit
3. Tenant-scoped (тільки свої курси)

## Очікуваний результат

GET /courses повертає список з pagination

## Як тестуємо

**Автоматизовано:** API test: create 5 courses, pagination (limit=2), tenant isolation

**Human control:** GET /courses — перевірити що повертаються тільки свої курси

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
