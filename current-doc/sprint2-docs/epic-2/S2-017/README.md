# S2-017: MaterialEntry repository

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 4h

---

## Мета

CRUD для MaterialEntry з pending receipt management і hash invalidation

## Що робимо

MaterialEntryRepository з CRUD, set/clear pending, update content, invalidate hash

## Як робимо

1. create(node_id, source_type, source_url, filename)
2. set_pending(entry_id, job_id) → pending_job_id + pending_since
3. complete_processing(entry_id, processed_content, processed_hash)
4. fail_processing(entry_id, error_message)
5. update_source(entry_id, new_url) → raw_hash=None (invalidation)
6. ensure_raw_hash(entry) → lazy calculation

## Очікуваний результат

Повний CRUD з lifecycle management

## Як тестуємо

**Автоматизовано:** Unit tests: full lifecycle RAW→PENDING→READY, hash invalidation, ensure_raw_hash

**Human control:** Немає (покривається unit tests + Epic 1 integration)

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
