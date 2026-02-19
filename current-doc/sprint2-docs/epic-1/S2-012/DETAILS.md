# S2-012: Worker integration tests — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 4h

---

## Мета

Повне тестове покриття job lifecycle і scheduling

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-011: Health check — додати Redis](../S2-011/README.md)



---

## Детальний план реалізації

1. Test fixtures: Redis instance (testcontainers або mock), ARQ worker
2. Test job lifecycle: queued → active → complete
3. Test retry з backoff при transient error
4. Test depends_on: job B стартує тільки після job A complete
5. Test work window: NORMAL job deferred, IMMEDIATE executes
6. Test callback: on_ingestion_complete triggered after job

---

## Очікуваний результат

Повний набір integration tests для worker lifecycle

---

## Тестування

### Автоматизовані тести

pytest проходить всі worker integration tests

### Ручний контроль (Human testing)

Review test coverage — чи покриті edge cases (worker crash, Redis disconnect, job timeout)

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
