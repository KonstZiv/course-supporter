# S2-008: Замінити BackgroundTasks → ARQ enqueue

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 3h

---

## Мета

Всі background tasks працюють через ARQ замість BackgroundTasks

## Що робимо

Замінити background_tasks.add_task() на arq_redis.enqueue_job(), оновити pending receipt в MaterialEntry

## Як робимо

1. Створити enqueue helper: створює Job в DB + enqueue в ARQ
2. Замінити всі background_tasks.add_task() на enqueue helper
3. При enqueue: оновити MaterialEntry.pending_job_id і pending_since
4. При завершенні: очистити pending_job_id, заповнити processed_*
5. Видалити BackgroundTasks з dependencies

## Очікуваний результат

Завантаження матеріалу створює ARQ job, MaterialEntry.state = PENDING з квитанцією

## Як тестуємо

**Автоматизовано:** Integration test: upload → job в Redis → worker обробляє → MaterialEntry.state = READY

**Human control:** Завантажити матеріал через API, перевірити що з'явився job, pending_job_id заповнений, після обробки — READY

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
