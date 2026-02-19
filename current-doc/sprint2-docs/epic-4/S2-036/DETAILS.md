# S2-036: Factory for heavy steps — Деталі для виконавця

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 2h

---

## Мета

Єдина точка створення heavy step implementations

## Контекст

Ця задача є частиною Epic "Heavy Steps Extraction" (2-3 дні).
Загальна ціль epic: Injectable heavy operations, serverless-ready boundary.

## Залежності

**Попередня задача:** [S2-035: Refactor processors as orchestrators](../S2-035/README.md)

**Наступна задача:** [S2-037: Heavy steps unit tests](../S2-037/README.md)



---

## Детальний план реалізації

1. Factory function: повертає TranscribeFunc, DescribeSlidesFunc, etc.
2. Зараз: local implementations
3. Потім: switch на lambda implementations по settings flag

---

## Очікуваний результат

Один рядок змінює local → lambda для всіх heavy steps

---

## Тестування

### Автоматизовані тести

Unit test: factory повертає callable-и з правильними signatures

### Ручний контроль (Human testing)

Немає

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
