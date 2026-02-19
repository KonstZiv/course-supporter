# S2-010: Job status API endpoint — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 2h

---

## Мета

GET /jobs/{id} повертає статус job-а з деталями

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-009: Ingestion completion callback](../S2-009/README.md)

**Наступна задача:** [S2-011: Health check — додати Redis](../S2-011/README.md)



---

## Детальний план реалізації

1. GET /api/v1/jobs/{job_id} → JobResponse schema
2. JobResponse: id, job_type, priority, status, timestamps, estimate, error_message
3. Tenant isolation через job.course_id → course.tenant_id
4. 404 якщо job не знайдений або не належить tenant-у

---

## Очікуваний результат

GET /jobs/{id} повертає повну інформацію про job

---

## Тестування

### Автоматизовані тести

Unit test: get existing job, 404 for wrong tenant, response schema validation

### Ручний контроль (Human testing)

Створити job → GET /jobs/{id} → перевірити що всі поля заповнені коректно

---

## Checklist перед PR

- [ ] Реалізація відповідає архітектурним рішенням Sprint 2 (AR-*)
- [ ] Код проходить `make check` (ruff + mypy + pytest)
- [ ] Unit tests написані і покривають основні сценарії
- [ ] Edge cases покриті (error handling, boundary values)
- [ ] Error messages зрозумілі і містять hints для користувача
- [ ] Human control points пройдені
- [ ] Документація оновлена якщо потрібно (ERD, API docs, sprint progress)
- [ ] Перевірено чи зміни впливають на наступні задачі — якщо так, оновити їх docs

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
