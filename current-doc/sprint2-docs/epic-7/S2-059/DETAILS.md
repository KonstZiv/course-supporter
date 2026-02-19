# S2-059: Auth & onboarding guide — Деталі для виконавця

**Epic:** EPIC-7 — Integration Documentation
**Оцінка:** 1h

---

## Мета

Інструкція як отримати API key і почати працювати

## Контекст

Ця задача є частиною Epic "Integration Documentation" (1-2 дні).
Загальна ціль epic: Зовнішня команда може почати інтеграцію. Публікується на docs site (Epic 0).

## Залежності

**Попередня задача:** [S2-058: API Reference update](../S2-058/README.md)

**Наступна задача:** [S2-060: Error handling guide](../S2-060/README.md)



---

## Детальний план реалізації

1. Як отримати API key
2. Header format: X-API-Key
3. Scopes: prep, check
4. Rate limits і як з ними працювати

---

## Очікуваний результат

Зрозуміла інструкція для початку роботи з API

---

## Тестування

### Автоматизовані тести

Немає

### Ручний контроль (Human testing)

Нова людина може отримати ключ і зробити перший запит

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
