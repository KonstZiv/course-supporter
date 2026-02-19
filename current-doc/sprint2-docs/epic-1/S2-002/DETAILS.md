# S2-002: ARQ worker setup + Settings — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 4h

---

## Мета

ARQ worker запускається, підключається до Redis, готовий приймати задачі

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-001: Redis в docker-compose (dev + prod)](../S2-001/README.md)

**Наступна задача:** [S2-003: Worker config через env](../S2-003/README.md)



---

## Детальний план реалізації

1. Додати arq і redis[hiredis] в залежності
2. Створити worker.py з class WorkerSettings (redis_settings, functions, max_jobs)
3. Redis connection pool через create_pool
4. Graceful shutdown handler (SIGTERM)
5. Logging конфігурація для worker
6. Перевірити: `arq course_supporter.worker.WorkerSettings` запускається

---

## Очікуваний результат

Worker запускається, логує підключення до Redis, чекає задачі

---

## Тестування

### Автоматизовані тести

Unit test: WorkerSettings має правильні defaults, connection pool створюється

### Ручний контроль (Human testing)

Запустити worker в терміналі, перевірити логи — підключення до Redis, очікування задач

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
