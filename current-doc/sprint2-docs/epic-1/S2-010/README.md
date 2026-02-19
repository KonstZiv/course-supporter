# S2-010: Job status API endpoint

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 2h

---

## Мета

GET /jobs/{id} повертає статус job-а з деталями

## Що робимо

Новий endpoint для перегляду статусу будь-якого job-а

## Як робимо

1. GET /api/v1/jobs/{job_id} → JobResponse schema
2. JobResponse: id, job_type, priority, status, timestamps, estimate, error_message
3. Tenant isolation через job.course_id → course.tenant_id
4. 404 якщо job не знайдений або не належить tenant-у

## Очікуваний результат

GET /jobs/{id} повертає повну інформацію про job

## Як тестуємо

**Автоматизовано:** Unit test: get existing job, 404 for wrong tenant, response schema validation

**Human control:** Створити job → GET /jobs/{id} → перевірити що всі поля заповнені коректно

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
