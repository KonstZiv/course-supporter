# S2-043: Batch create endpoint (partial success) — Деталі для виконавця

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 4h

---

## Мета

POST batch маппінгів з per-item результатами і hints

## Контекст

Ця задача є частиною Epic "SlideVideoMapping — Redesign" (3-4 дні).
Загальна ціль epic: Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

## Залежності

**Попередня задача:** [S2-042: Auto-revalidation on ingestion complete](../S2-042/README.md)

**Наступна задача:** [S2-044: Mapping CRUD endpoints](../S2-044/README.md)



---

## Детальний план реалізації

1. Accept array of mapping objects
2. Validate each independently
3. Create valid ones, skip invalid
4. Return per-item results: status (created/failed), errors, warnings
5. Response hints: resubmit guidance, batch_size recommendation

---

## Очікуваний результат

Batch upload з partial success і детальними per-item повідомленнями

---

## Тестування

### Автоматизовані тести

API tests: full success, partial success, full failure, empty batch

### Ручний контроль (Human testing)

Подати batch 10 маппінгів (8 ok, 2 invalid) → перевірити response — per-item statuses, error hints

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
