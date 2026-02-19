# S2-060: Error handling guide — Деталі для виконавця

**Epic:** EPIC-7 — Integration Documentation
**Оцінка:** 2h

---

## Мета

Всі коди помилок документовані з retry стратегіями

## Контекст

Ця задача є частиною Epic "Integration Documentation" (1-2 дні).
Загальна ціль epic: Зовнішня команда може почати інтеграцію. Публікується на docs site (Epic 0).

## Залежності

**Попередня задача:** [S2-059: Auth & onboarding guide](../S2-059/README.md)



---

## Детальний план реалізації

1. Список всіх HTTP status codes і їх значення
2. Error response format
3. Retry стратегії для кожного типу помилки
4. Polling patterns для async operations
5. Rate limit handling

---

## Очікуваний результат

Розробник знає як обробляти всі можливі помилки

---

## Тестування

### Автоматизовані тести

Немає

### Ручний контроль (Human testing)

Перевірити що всі error codes з API покриті в документації

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
