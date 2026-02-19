# S2-058: API Reference update — Деталі для виконавця

**Epic:** EPIC-7 — Integration Documentation
**Оцінка:** 2h

---

## Мета

Повна API reference з прикладами запитів і відповідей

## Контекст

Ця задача є частиною Epic "Integration Documentation" (1-2 дні).
Загальна ціль epic: Зовнішня команда може почати інтеграцію. Публікується на docs site (Epic 0).

## Залежності

**Попередня задача:** [S2-057: Flow Guide](../S2-057/README.md)

**Наступна задача:** [S2-059: Auth & onboarding guide](../S2-059/README.md)



---

## Детальний план реалізації

1. Зібрати всі endpoints з OpenAPI schema
2. Додати приклади request/response для кожного
3. Описати query parameters, headers, body schemas

---

## Очікуваний результат

Повна API reference на docs site

---

## Тестування

### Автоматизовані тести

OpenAPI schema validation

### Ручний контроль (Human testing)

Перевірити кожен приклад — чи працює copy-paste

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
