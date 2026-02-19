# S2-014: MaterialEntry ORM model — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 3h

---

## Мета

ORM модель матеріалу з raw/processed layers і pending receipt

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Попередня задача:** [S2-013: MaterialNode ORM model](../S2-013/README.md)

**Наступна задача:** [S2-015: MaterialState derived property](../S2-015/README.md)



---

## Детальний план реалізації

1. MaterialEntry з усіма полями згідно AR-2
2. FK на MaterialNode (node_id) і Job (pending_job_id)
3. Relationships з MaterialNode і Job

---

## Очікуваний результат

MaterialEntry ORM працює, всі поля доступні

---

## Тестування

### Автоматизовані тести

Unit test: create entry, set/clear pending receipt, update processed layer

### Ручний контроль (Human testing)

Перевірити в DB структуру таблиці — всі колонки відповідають AR-2

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
