# S2-004: Work Window service — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 4h

---

## Мета

Сервіс визначає чи зараз 'робочий час' для heavy tasks

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-003: Worker config через env](../S2-003/README.md)

**Наступна задача:** [S2-005: Job priorities (IMMEDIATE/NORMAL)](../S2-005/README.md)



---

## Детальний план реалізації

1. WorkWindow(start: str, end: str, tz: str, enabled: bool) з HH:MM parsing
2. is_active_now() → bool (з урахуванням timezone)
3. next_start() → datetime (наступне відкриття вікна)
4. remaining_today() → timedelta (скільки лишилось до закриття)
5. Підтримка overnight windows (start=22:00, end=06:00)
6. enabled=False → is_active_now() завжди True (24/7 mode)

---

## Очікуваний результат

WorkWindow правильно визначає чи зараз робочий час з урахуванням timezone і overnight

---

## Тестування

### Автоматизовані тести

Unit tests: звичайне вікно (02:00-06:30), overnight вікно (22:00-06:00), disabled mode (24/7), timezone handling, next_start() через midnight, remaining_today()

### Ручний контроль (Human testing)

Встановити вікно на найближчі 5 хвилин, перевірити що is_active_now() змінюється в правильний момент

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
