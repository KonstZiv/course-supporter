# S2-015: MaterialState derived property — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 1h

---

## Мета

Стан матеріалу визначається автоматично з полів entry

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Попередня задача:** [S2-014: MaterialEntry ORM model](../S2-014/README.md)

**Наступна задача:** [S2-016: MaterialNode repository](../S2-016/README.md)



---

## Детальний план реалізації

1. MaterialState(StrEnum): RAW, PENDING, READY, INTEGRITY_BROKEN, ERROR
2. @property state з логікою пріоритетів: ERROR > PENDING > RAW > INTEGRITY_BROKEN > READY

---

## Очікуваний результат

entry.state правильно відображає поточний стан

---

## Тестування

### Автоматизовані тести

Unit tests: всі 5 станів, перехід між станами, edge cases (error + pending одночасно)

### Ручний контроль (Human testing)

Немає (повністю покривається unit tests)

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
