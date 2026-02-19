# S2-003: Worker config через env — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 2h

---

## Мета

Всі параметри worker-а конфігуруються через змінні оточення

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-002: ARQ worker setup + Settings](../S2-002/README.md)

**Наступна задача:** [S2-004: Work Window service](../S2-004/README.md)



---

## Детальний план реалізації

1. Додати в Settings: worker_max_jobs, worker_job_timeout, worker_max_tries
2. Додати в Settings: worker_heavy_window_start/end/enabled/tz
3. Додати WORKER_* змінні в .env.example
4. WorkerSettings читає з Settings instance

---

## Очікуваний результат

Зміна WORKER_MAX_JOBS=3 в .env → worker використовує max_jobs=3

---

## Тестування

### Автоматизовані тести

Unit test: Settings парсить WORKER_* змінні, defaults правильні

### Ручний контроль (Human testing)

Змінити WORKER_MAX_JOBS в .env, перезапустити worker, перевірити в логах нове значення

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
