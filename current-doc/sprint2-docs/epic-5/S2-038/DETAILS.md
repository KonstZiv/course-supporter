# S2-038: SlideVideoMapping ORM redesign — Деталі для виконавця

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 3h

---

## Мета

Нова модель з FK на MaterialEntry і validation state

## Контекст

Ця задача є частиною Epic "SlideVideoMapping — Redesign" (3-4 дні).
Загальна ціль epic: Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

## Залежності

**Наступна задача:** [S2-039: MappingValidationService — structural validation (Level 1)](../S2-039/README.md)



---

## Детальний план реалізації

1. Нові поля: presentation_entry_id (FK), video_entry_id (FK), validation_state, blocking_factors (JSONB), validation_errors (JSONB), validated_at
2. MappingValidationState enum
3. Alembic migration

---

## Очікуваний результат

Маппінг зв'язує конкретну презентацію з конкретним відео з validation tracking

---

## Тестування

### Автоматизовані тести

Unit test: create mapping з FK, validation state transitions

### Ручний контроль (Human testing)

Перевірити в DB структуру таблиці

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
