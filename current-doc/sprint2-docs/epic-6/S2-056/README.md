# S2-056: Structure generation tests

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 4h

---

## Мета

Повне тестове покриття для generation pipeline

## Що робимо

Integration і unit tests для всього Epic 6

## Як робимо

1. Full pipeline mock: upload → ingestion → generation → snapshot
2. Idempotency: same fingerprint → 200
3. Conflict: overlapping subtrees → 409
4. Readiness: no ready materials → 422
5. Cascade: RAW materials → auto-ingestion → generation
6. Free vs guided: different outputs

## Очікуваний результат

Повне покриття generation pipeline

## Як тестуємо

**Автоматизовано:** pytest

**Human control:** Review — чи покриті всі сценарії з AR-6

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
