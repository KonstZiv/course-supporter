# S2-040: MappingValidationService — content validation (Level 2) — Деталі для виконавця

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 3h

---

## Мета

Перевірка slide_number і timecode в межах реального контенту

## Контекст

Ця задача є частиною Epic "SlideVideoMapping — Redesign" (3-4 дні).
Загальна ціль epic: Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

## Залежності

**Попередня задача:** [S2-039: MappingValidationService — structural validation (Level 1)](../S2-039/README.md)

**Наступна задача:** [S2-041: MappingValidationService — deferred validation (Level 3)](../S2-041/README.md)



---

## Детальний план реалізації

1. Extract slide_count з processed_content → перевірити slide_number
2. Extract duration з processed_content → перевірити timecodes
3. Error messages з допустимими діапазонами

---

## Очікуваний результат

Контентні помилки ловляться коли матеріали оброблені

---

## Тестування

### Автоматизовані тести

Unit tests: slide out of range, timecode overflow, boundary values

### Ручний контроль (Human testing)

Подати маппінг slide=100 для презентації з 30 слайдів → перевірити hint '1–30'

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
