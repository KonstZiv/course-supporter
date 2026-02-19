# S2-001: Redis в docker-compose (dev + prod)

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 2h

---

## Мета

Redis доступний як сервіс в dev і prod оточеннях

## Що робимо

Додати redis:7-alpine в docker-compose.dev.yaml і docker-compose.prod.yaml

## Як робимо

1. Додати redis service: image redis:7-alpine, appendonly yes, healthcheck (redis-cli ping)
2. Volume redis-data для persistence
3. Network — default (та ж мережа що API і postgres)
4. Додати REDIS_URL в .env.example і settings

## Очікуваний результат

`docker-compose up redis` → Redis healthy, `redis-cli ping` → PONG

## Як тестуємо

**Автоматизовано:** Health check в docker-compose (redis-cli ping)

**Human control:** docker-compose up, перевірити logs — Redis started, redis-cli з хост-машини підключається

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
