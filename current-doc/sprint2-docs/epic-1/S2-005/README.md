# S2-005: Job priorities (IMMEDIATE/NORMAL)

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 2h

---

## Мета

Heavy jobs чекають робочого вікна, light jobs виконуються завжди

## Що робимо

Реалізувати JobPriority enum і перевірку вікна перед виконанням NORMAL jobs

## Як робимо

1. JobPriority enum: IMMEDIATE, NORMAL
2. Wrapper для ARQ task functions: перевірка priority + window
3. NORMAL + window inactive → Retry(defer=window.next_start())
4. IMMEDIATE → виконується завжди

## Очікуваний результат

NORMAL job відкладається до наступного вікна, IMMEDIATE виконується одразу

## Як тестуємо

**Автоматизовано:** Unit test: NORMAL job outside window → Retry raised з правильним defer, IMMEDIATE → виконується

**Human control:** Встановити вікно в майбутнє, подати NORMAL job → перевірити що чекає. Подати IMMEDIATE → виконується одразу

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
