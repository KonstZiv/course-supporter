# S2-018: Alembic migration: new tables + data migration — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 4h

---

## Мета

Database schema оновлена, існуючі дані мігровані

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Попередня задача:** [S2-017: MaterialEntry repository](../S2-017/README.md)

**Наступна задача:** [S2-019: Tree API endpoints (nodes)](../S2-019/README.md)



---

## Детальний план реалізації

1. Migration: CREATE material_nodes, material_entries
2. Data migration: для кожного Course створити root MaterialNode
3. Перенести source_materials → material_entries (через root node)
4. Downgrade: зворотна міграція
5. Тестувати на копії production DB

---

## Очікуваний результат

alembic upgrade head працює, дані збережені, downgrade працює

---

## Тестування

### Автоматизовані тести

Migration test: upgrade → verify data → downgrade → verify rollback

### Ручний контроль (Human testing)

Запустити міграцію на staging з копією production даних, перевірити що всі матеріали на місці

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
