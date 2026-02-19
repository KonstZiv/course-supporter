# S2-002: ARQ worker setup + Settings

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 4h

---

## Мета

ARQ worker запускається, підключається до Redis, готовий приймати задачі

## Що робимо

Створити course_supporter/worker.py з WorkerSettings, connection pool, graceful shutdown

## Як робимо

1. Додати arq і redis[hiredis] в залежності
2. Створити worker.py з class WorkerSettings (redis_settings, functions, max_jobs)
3. Redis connection pool через create_pool
4. Graceful shutdown handler (SIGTERM)
5. Logging конфігурація для worker
6. Перевірити: `arq course_supporter.worker.WorkerSettings` запускається

## Очікуваний результат

Worker запускається, логує підключення до Redis, чекає задачі

## Як тестуємо

**Автоматизовано:** Unit test: WorkerSettings має правильні defaults, connection pool створюється

**Human control:** Запустити worker в терміналі, перевірити логи — підключення до Redis, очікування задач

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
