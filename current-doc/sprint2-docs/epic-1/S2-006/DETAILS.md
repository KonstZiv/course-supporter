# S2-006: Job ORM model + repository — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 3h

---

## Мета

Job tracking в PostgreSQL з CRUD і status transitions

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-005: Job priorities (IMMEDIATE/NORMAL)](../S2-005/README.md)

**Наступна задача:** [S2-007: Queue estimate service](../S2-007/README.md)



---

## Детальний план реалізації

1. Job model: id, course_id, node_id, job_type, priority, status, arq_job_id, input_params, result_ref, depends_on, error_message, timestamps
2. JobRepository: create, get_by_id, update_status, get_active_for_course, count_pending
3. Status transitions: queued→active→complete|failed
4. Alembic migration для jobs table

---

## Очікуваний результат

Job-и зберігаються в PostgreSQL, можна query по course_id, status, node_id

---

## Тестування

### Автоматизовані тести

Unit tests: CRUD, status transitions, filtering by course/status/node

### Ручний контроль (Human testing)

Створити job через API, перевірити в DB що запис є з правильними полями

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
