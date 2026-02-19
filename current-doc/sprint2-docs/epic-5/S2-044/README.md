# S2-044: Mapping CRUD endpoints

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 2h

---

## Мета

GET list і DELETE для маппінгів

## Що робимо

GET /nodes/{node_id}/slide-mapping і DELETE /slide-mapping/{id}

## Як робимо

1. GET: list all mappings for node з validation_state
2. DELETE: видалити маппінг
3. Tenant isolation

## Очікуваний результат

Повний CRUD для маппінгів

## Як тестуємо

**Автоматизовано:** API tests: list, delete, tenant isolation

**Human control:** GET маппінги для node → перевірити що validation_state видимий

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
