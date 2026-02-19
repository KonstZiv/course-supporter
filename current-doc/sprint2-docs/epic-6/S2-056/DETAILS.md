# S2-056: Structure generation tests — Деталі для виконавця

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 4h

---

## Мета

Повне тестове покриття для generation pipeline

## Контекст

Ця задача є частиною Epic "Structure Generation Pipeline" (3-4 дні).
Загальна ціль epic: Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

## Залежності

**Попередня задача:** [S2-055: Mapping warnings in generation](../S2-055/README.md)



---

## Детальний план реалізації

1. Full pipeline mock: upload → ingestion → generation → snapshot
2. Idempotency: same fingerprint → 200
3. Conflict: overlapping subtrees → 409
4. Readiness: no ready materials → 422
5. Cascade: RAW materials → auto-ingestion → generation
6. Free vs guided: different outputs

---

## Очікуваний результат

Повне покриття generation pipeline

---

## Тестування

### Автоматизовані тести

pytest

### Ручний контроль (Human testing)

Review — чи покриті всі сценарії з AR-6

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
