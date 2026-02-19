# S2-039: MappingValidationService — structural validation (Level 1) — Деталі для виконавця

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 3h

---

## Мета

Перевірка що матеріали існують, належать вузлу, мають правильний тип

## Контекст

Ця задача є частиною Epic "SlideVideoMapping — Redesign" (3-4 дні).
Загальна ціль epic: Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

## Залежності

**Попередня задача:** [S2-038: SlideVideoMapping ORM redesign](../S2-038/README.md)

**Наступна задача:** [S2-040: MappingValidationService — content validation (Level 2)](../S2-040/README.md)



---

## Детальний план реалізації

1. Check entry exists and node_id matches
2. Check source_type = presentation/video
3. Validate timecode format (HH:MM:SS)
4. Detailed error messages з hints

---

## Очікуваний результат

Структурні помилки ловляться до створення маппінгу

---

## Тестування

### Автоматизовані тести

Unit tests: wrong node, wrong type, invalid timecode, missing entry

### Ручний контроль (Human testing)

Подати маппінг з wrong type → перевірити що error message зрозумілий і корисний

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
