# S2-013: MaterialNode ORM model — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 2h

---

## Мета

ORM модель для вузлів дерева матеріалів

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Наступна задача:** [S2-014: MaterialEntry ORM model](../S2-014/README.md)



---

## Детальний план реалізації

1. MaterialNode(id, course_id, parent_id→self, title, description, order, node_fingerprint)
2. Relationships: children, materials, parent
3. Cascade delete для children

---

## Очікуваний результат

MaterialNode ORM працює, можна створювати вкладені вузли

---

## Тестування

### Автоматизовані тести

Unit test: create node, create child, self-ref FK працює

### Ручний контроль (Human testing)

Перевірити в DB що записи створюються з правильними parent_id

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
