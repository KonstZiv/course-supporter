# S2-008: Замінити BackgroundTasks → ARQ enqueue — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 3h

---

## Мета

Всі background tasks працюють через ARQ замість BackgroundTasks

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-007: Queue estimate service](../S2-007/README.md)

**Наступна задача:** [S2-009: Ingestion completion callback](../S2-009/README.md)



---

## Детальний план реалізації

1. Створити enqueue helper: створює Job в DB + enqueue в ARQ
2. Замінити всі background_tasks.add_task() на enqueue helper
3. При enqueue: оновити MaterialEntry.pending_job_id і pending_since
4. При завершенні: очистити pending_job_id, заповнити processed_*
5. Видалити BackgroundTasks з dependencies

---

## Очікуваний результат

Завантаження матеріалу створює ARQ job, MaterialEntry.state = PENDING з квитанцією

---

## Тестування

### Автоматизовані тести

Integration test: upload → job в Redis → worker обробляє → MaterialEntry.state = READY

### Ручний контроль (Human testing)

Завантажити матеріал через API, перевірити що з'явився job, pending_job_id заповнений, після обробки — READY

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
