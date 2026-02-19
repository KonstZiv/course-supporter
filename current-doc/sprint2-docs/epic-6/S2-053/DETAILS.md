# S2-053: Structure generation API — Деталі для виконавця

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 4h

---

## Мета

Повний API для trigger і перегляду результатів генерації

## Контекст

Ця задача є частиною Epic "Structure Generation Pipeline" (3-4 дні).
Загальна ціль epic: Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

## Залежності

**Попередня задача:** [S2-052: Free vs Guided mode](../S2-052/README.md)

**Наступна задача:** [S2-054: MergeStep refactor — tree-aware](../S2-054/README.md)



---

## Детальний план реалізації

1. POST /courses/{id}/structure/generate і /nodes/{node_id}/structure/generate
2. Response codes: 200 (existing), 202 (new), 409 (conflict), 422 (no ready materials)
3. GET /structure — latest snapshot
4. GET /structure/jobs — generation jobs list

---

## Очікуваний результат

Повний API для generation workflow

---

## Тестування

### Автоматизовані тести

API tests: all 4 response codes, GET status, GET result, tenant isolation

### Ручний контроль (Human testing)

Повний flow через API: create → upload → generate → poll → get result

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
