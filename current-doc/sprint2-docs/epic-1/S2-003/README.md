# S2-003: Worker config через env

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 2h

---

## Мета

Всі параметри worker-а конфігуруються через змінні оточення

## Що робимо

Додати worker-specific settings в Settings (BaseSettings)

## Як робимо

1. Додати в Settings: worker_max_jobs, worker_job_timeout, worker_max_tries
2. Додати в Settings: worker_heavy_window_start/end/enabled/tz
3. Додати WORKER_* змінні в .env.example
4. WorkerSettings читає з Settings instance

## Очікуваний результат

Зміна WORKER_MAX_JOBS=3 в .env → worker використовує max_jobs=3

## Як тестуємо

**Автоматизовано:** Unit test: Settings парсить WORKER_* змінні, defaults правильні

**Human control:** Змінити WORKER_MAX_JOBS в .env, перезапустити worker, перевірити в логах нове значення

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
