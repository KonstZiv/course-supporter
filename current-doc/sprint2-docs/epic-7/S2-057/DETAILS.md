# S2-057: Flow Guide — Деталі для виконавця

**Epic:** EPIC-7 — Integration Documentation
**Оцінка:** 3h

---

## Мета

Повний сценарій від створення курсу до отримання структури

## Контекст

Ця задача є частиною Epic "Integration Documentation" (1-2 дні).
Загальна ціль epic: Зовнішня команда може почати інтеграцію. Публікується на docs site (Epic 0).

## Залежності

**Наступна задача:** [S2-058: API Reference update](../S2-058/README.md)



---

## Детальний план реалізації

1. Описати кожен крок: create course → add nodes → upload materials → generate
2. Curl приклади для кожного endpoint
3. Описати polling pattern для async operations
4. Описати error recovery scenarios

---

## Очікуваний результат

Новий розробник може пройти повний flow за 30 хвилин

---

## Тестування

### Автоматизовані тести

Немає (контентна задача)

### Ручний контроль (Human testing)

Дати guide новій людині — чи проходить flow без питань

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
