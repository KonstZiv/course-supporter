# S2-049: Conflict detection (subtree overlap) — Деталі для виконавця

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 3h

---

## Мета

Визначити чи новий запит на генерацію перетинається з active job

## Контекст

Ця задача є частиною Epic "Structure Generation Pipeline" (3-4 дні).
Загальна ціль epic: Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

## Залежності

**Попередня задача:** [S2-048: Subtree readiness check](../S2-048/README.md)

**Наступна задача:** [S2-050: Generate structure ARQ task](../S2-050/README.md)



---

## Детальний план реалізації

1. Get active generation jobs for course
2. Для кожного: is_ancestor_or_same(active_node, target_node) в обидва боки
3. _is_ancestor_or_same: traverse parent_id chain

---

## Очікуваний результат

Conflict detection працює для всіх комбінацій (course↔node, node↔node, siblings)

---

## Тестування

### Автоматизовані тести

Unit tests: all combinations from AR-6 table (course-course, node-child, siblings)

### Ручний контроль (Human testing)

Немає (таблиця сценаріїв покрита unit tests)

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
