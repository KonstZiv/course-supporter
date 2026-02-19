# S2-007: Queue estimate service — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 4h

---

## Мета

При submit job — розрахунок estimated start/complete з урахуванням черги і вікна

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-006: Job ORM model + repository](../S2-006/README.md)

**Наступна задача:** [S2-008: Замінити BackgroundTasks → ARQ enqueue](../S2-008/README.md)



---

## Детальний план реалізації

1. QueueEstimate dataclass: position_in_queue, estimated_start, estimated_complete, next_window_start, queue_summary
2. estimate_job(): count_pending × avg_completion_time
3. Window-aware: якщо поза вікном → next_start + queue time
4. avg_completion_time з jobs history (або default для нових систем)
5. Обробка випадку коли черга не вміщується в одне вікно (overflow на наступний день)

---

## Очікуваний результат

estimate_job() повертає адекватні прогнози з урахуванням черги і вікна

---

## Тестування

### Автоматизовані тести

Unit tests: порожня черга (start=now), 5 jobs в черзі, поза вікном (start=next_window+queue), overflow на наступний день, 24/7 mode, default avg коли немає history

### Ручний контроль (Human testing)

Додати 3 job-и в чергу, перевірити що estimated_at для 4-го адекватний (queue_position × avg_time)

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
