# S2-041: MappingValidationService — deferred validation (Level 3) — Деталі для виконавця

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 4h

---

## Мета

Маппінги приймаються коли матеріали ще не оброблені, валідуються пізніше

## Контекст

Ця задача є частиною Epic "SlideVideoMapping — Redesign" (3-4 дні).
Загальна ціль epic: Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

## Залежності

**Попередня задача:** [S2-040: MappingValidationService — content validation (Level 2)](../S2-040/README.md)

**Наступна задача:** [S2-042: Auto-revalidation on ingestion complete](../S2-042/README.md)



---

## Детальний план реалізації

1. Якщо матеріал не READY → записати blocking_factor
2. blocking_factor: type, material_entry_id, filename, state, message, blocked_checks
3. validation_state = pending_validation
4. Коли матеріал стає READY → зняти blocker → спробувати Level 2 валідацію

---

## Очікуваний результат

Маппінги з необробленими матеріалами приймаються з чітким описом що блокує

---

## Тестування

### Автоматизовані тести

Unit tests: create with pending material → blocking_factor, material becomes READY → validated, material ERROR → blocker updated

### Ручний контроль (Human testing)

Подати маппінг з pending матеріалом → перевірити blocking_factors JSON — чи зрозумілий message

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
