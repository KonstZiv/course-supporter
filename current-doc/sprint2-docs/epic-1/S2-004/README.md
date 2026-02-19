# S2-004: Work Window service

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 4h

---

## Мета

Сервіс визначає чи зараз 'робочий час' для heavy tasks

## Що робимо

Створити WorkWindow class з is_active_now(), next_start(), remaining_today()

## Як робимо

1. WorkWindow(start: str, end: str, tz: str, enabled: bool) з HH:MM parsing
2. is_active_now() → bool (з урахуванням timezone)
3. next_start() → datetime (наступне відкриття вікна)
4. remaining_today() → timedelta (скільки лишилось до закриття)
5. Підтримка overnight windows (start=22:00, end=06:00)
6. enabled=False → is_active_now() завжди True (24/7 mode)

## Очікуваний результат

WorkWindow правильно визначає чи зараз робочий час з урахуванням timezone і overnight

## Як тестуємо

**Автоматизовано:** Unit tests: звичайне вікно (02:00-06:30), overnight вікно (22:00-06:00), disabled mode (24/7), timezone handling, next_start() через midnight, remaining_today()

**Human control:** Встановити вікно на найближчі 5 хвилин, перевірити що is_active_now() змінюється в правильний момент

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
