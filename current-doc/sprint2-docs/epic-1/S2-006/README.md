# S2-006: Job ORM model + repository

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 3h

---

## Мета

Job tracking в PostgreSQL з CRUD і status transitions

## Що робимо

Створити Job ORM model і JobRepository

## Як робимо

1. Job model: id, course_id, node_id, job_type, priority, status, arq_job_id, input_params, result_ref, depends_on, error_message, timestamps
2. JobRepository: create, get_by_id, update_status, get_active_for_course, count_pending
3. Status transitions: queued→active→complete|failed
4. Alembic migration для jobs table

## Очікуваний результат

Job-и зберігаються в PostgreSQL, можна query по course_id, status, node_id

## Як тестуємо

**Автоматизовано:** Unit tests: CRUD, status transitions, filtering by course/status/node

**Human control:** Створити job через API, перевірити в DB що запис є з правильними полями

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
