# S2-048: Subtree readiness check — Деталі для виконавця

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 2h

---

## Мета

Знайти stale матеріали (RAW, INTEGRITY_BROKEN) в піддереві

## Контекст

Ця задача є частиною Epic "Structure Generation Pipeline" (3-4 дні).
Загальна ціль epic: Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

## Залежності

**Попередня задача:** [S2-047: CourseStructureSnapshot ORM + repository](../S2-047/README.md)

**Наступна задача:** [S2-049: Conflict detection (subtree overlap)](../S2-049/README.md)



---

## Детальний план реалізації

1. Обійти піддерево від node_id
2. Знайти MaterialEntry де state in (RAW, INTEGRITY_BROKEN)
3. Повернути список з деталями (id, filename, state, node_title)

---

## Очікуваний результат

Швидка перевірка готовності піддерева до генерації

---

## Тестування

### Автоматизовані тести

Unit tests: all ready, some stale, nested stale, empty tree

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
