# S2-005: Job priorities (IMMEDIATE/NORMAL) — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 2h

---

## Мета

Heavy jobs чекають робочого вікна, light jobs виконуються завжди

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-004: Work Window service](../S2-004/README.md)

**Наступна задача:** [S2-006: Job ORM model + repository](../S2-006/README.md)



---

## Детальний план реалізації

1. JobPriority enum: IMMEDIATE, NORMAL
2. Wrapper для ARQ task functions: перевірка priority + window
3. NORMAL + window inactive → Retry(defer=window.next_start())
4. IMMEDIATE → виконується завжди

---

## Очікуваний результат

NORMAL job відкладається до наступного вікна, IMMEDIATE виконується одразу

---

## Тестування

### Автоматизовані тести

Unit test: NORMAL job outside window → Retry raised з правильним defer, IMMEDIATE → виконується

### Ручний контроль (Human testing)

Встановити вікно в майбутнє, подати NORMAL job → перевірити що чекає. Подати IMMEDIATE → виконується одразу

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
