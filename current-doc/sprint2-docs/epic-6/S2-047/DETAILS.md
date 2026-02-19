# S2-047: CourseStructureSnapshot ORM + repository — Деталі для виконавця

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 3h

---

## Мета

Snapshot зберігає результат генерації з прив'язкою до fingerprint і scope

## Контекст

Ця задача є частиною Epic "Structure Generation Pipeline" (3-4 дні).
Загальна ціль epic: Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

## Залежності

**Наступна задача:** [S2-048: Subtree readiness check](../S2-048/README.md)



---

## Детальний план реалізації

1. Model: course_id, node_id (nullable), node_fingerprint, mode, structure (JSONB), LLM metadata
2. Unique constraint: (course_id, node_id, fingerprint, mode)
3. Repository: create, find_by_identity, get_latest

---

## Очікуваний результат

Snapshots зберігаються з прив'язкою до fingerprint для idempotency

---

## Тестування

### Автоматизовані тести

Unit tests: CRUD, unique constraint violation, find by identity

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
