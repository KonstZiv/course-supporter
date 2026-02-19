# S2-042: Auto-revalidation on ingestion complete — Деталі для виконавця

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 3h

---

## Мета

Завершення ingestion автоматично перевалідовує pending маппінги

## Контекст

Ця задача є частиною Epic "SlideVideoMapping — Redesign" (3-4 дні).
Загальна ціль epic: Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

## Залежності

**Попередня задача:** [S2-041: MappingValidationService — deferred validation (Level 3)](../S2-041/README.md)

**Наступна задача:** [S2-043: Batch create endpoint (partial success)](../S2-043/README.md)



---

## Детальний план реалізації

1. find_blocked_by(material_entry_id) → list of pending mappings
2. Для кожного: recalculate blocking_factors
3. Якщо всі блокери зняті → run Level 2 validation
4. Update validation_state accordingly

---

## Очікуваний результат

Ingestion complete → pending маппінги автоматично валідуються

---

## Тестування

### Автоматизовані тести

Integration test: create mapping (pending) → ingestion completes → mapping becomes validated

### Ручний контроль (Human testing)

Створити маппінг з pending презентацією → дочекатись ingestion → GET mapping → state = validated

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
