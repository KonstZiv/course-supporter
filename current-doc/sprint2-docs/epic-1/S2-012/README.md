# S2-012: Worker integration tests

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 4h

---

## Мета

Повне тестове покриття job lifecycle і scheduling

## Що робимо

Integration tests для ARQ worker з mock processors

## Як робимо

1. Test fixtures: Redis instance (testcontainers або mock), ARQ worker
2. Test job lifecycle: queued → active → complete
3. Test retry з backoff при transient error
4. Test depends_on: job B стартує тільки після job A complete
5. Test work window: NORMAL job deferred, IMMEDIATE executes
6. Test callback: on_ingestion_complete triggered after job

## Очікуваний результат

Повний набір integration tests для worker lifecycle

## Як тестуємо

**Автоматизовано:** pytest проходить всі worker integration tests

**Human control:** Review test coverage — чи покриті edge cases (worker crash, Redis disconnect, job timeout)

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
