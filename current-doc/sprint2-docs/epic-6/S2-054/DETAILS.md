# S2-054: MergeStep refactor — tree-aware — Деталі для виконавця

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 3h

---

## Мета

Merge враховує ієрархію MaterialNode при формуванні context

## Контекст

Ця задача є частиною Epic "Structure Generation Pipeline" (3-4 дні).
Загальна ціль epic: Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

## Залежності

**Попередня задача:** [S2-053: Structure generation API](../S2-053/README.md)

**Наступна задача:** [S2-055: Mapping warnings in generation](../S2-055/README.md)



---

## Детальний план реалізації

1. MergeStep.merge(tree: MaterialNode) замість flat list
2. CourseContext включає tree structure (node titles, nesting)
3. В guided mode: tree structure = constraint для agent

---

## Очікуваний результат

ArchitectAgent отримує повний context про структуру дерева

---

## Тестування

### Автоматизовані тести

Unit test: MergeStep з nested tree → CourseContext містить hierarchy

### Ручний контроль (Human testing)

Перевірити що generated structure відображає input tree hierarchy (guided mode)

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
