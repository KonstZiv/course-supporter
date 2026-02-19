# S2-016: MaterialNode repository — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 4h

---

## Мета

CRUD + tree operations для MaterialNode

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Попередня задача:** [S2-015: MaterialState derived property](../S2-015/README.md)

**Наступна задача:** [S2-017: MaterialEntry repository](../S2-017/README.md)



---

## Детальний план реалізації

1. create(course_id, parent_id, title) з order auto-increment
2. get_tree(course_id) — recursive eager load
3. move(node_id, new_parent_id) з валідацією циклів
4. reorder(node_id, new_order) з shift siblings
5. delete з cascade

---

## Очікуваний результат

Повний CRUD для tree nodes з валідацією

---

## Тестування

### Автоматизовані тести

Unit tests: create, move (з cycle detection), reorder, cascade delete, get_tree depth 5

### Ручний контроль (Human testing)

Через DB client створити дерево 4 рівні → get_tree → перевірити повноту

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
