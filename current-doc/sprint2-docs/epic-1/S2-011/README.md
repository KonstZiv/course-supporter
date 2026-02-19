# S2-011: Health check — додати Redis

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 1h

---

## Мета

/health перевіряє Redis connectivity

## Що робимо

Розширити health endpoint перевіркою Redis

## Як робимо

1. Додати Redis ping в health check
2. Response: { db: ok, s3: ok, redis: ok }
3. Якщо Redis недоступний — HTTP 503 з деталями

## Очікуваний результат

/health повертає статус DB + S3 + Redis

## Як тестуємо

**Автоматизовано:** Unit test: health з mock Redis (ok і fail scenarios)

**Human control:** Зупинити Redis → GET /health → 503 з redis: error. Запустити → 200 з redis: ok

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
