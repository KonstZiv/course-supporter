# S2-021: Course detail response — tree structure — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 3h

---

## Мета

GET /courses/{id} повертає повне дерево з матеріалами і fingerprints

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Попередня задача:** [S2-020: Materials endpoint refactor](../S2-020/README.md)

**Наступна задача:** [S2-022: List courses endpoint](../S2-022/README.md)



---

## Детальний план реалізації

1. CourseDetailResponse schema з nested NodeResponse
2. NodeResponse: id, title, children[], materials[], fingerprint
3. MaterialEntryResponse: id, filename, state, fingerprint, pending info
4. Recursive serialization дерева

---

## Очікуваний результат

GET /courses/{id} повертає повне дерево зі станами і fingerprints

---

## Тестування

### Автоматизовані тести

API test: create tree with materials → GET → verify full structure in response

### Ручний контроль (Human testing)

GET /courses/{id} — JSON response має правильну вкладеність, стани матеріалів коректні

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
