# S2-023: Tree + MaterialEntry unit tests — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 5h

---

## Мета

Повне тестове покриття для tree і entry operations

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Попередня задача:** [S2-022: List courses endpoint](../S2-022/README.md)



---

## Детальний план реалізації

1. MaterialNode: CRUD, move з cycle detection, cascade delete, deep nesting (5+ рівнів)
2. MaterialEntry: state transitions, pending lifecycle, hash invalidation
3. API: full flow (create course → tree → materials → verify)
4. Edge cases: empty tree, single node, very deep tree

---

## Очікуваний результат

Всі тести зелені, coverage > 90% для нових модулів

---

## Тестування

### Автоматизовані тести

pytest з coverage report

### Ручний контроль (Human testing)

Review test cases — чи покриті edge cases, чи тести читабельні

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
