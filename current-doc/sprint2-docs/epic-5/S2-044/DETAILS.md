# S2-044: Mapping CRUD endpoints — Деталі для виконавця

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 2h

---

## Мета

GET list і DELETE для маппінгів

## Контекст

Ця задача є частиною Epic "SlideVideoMapping — Redesign" (3-4 дні).
Загальна ціль epic: Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

## Залежності

**Попередня задача:** [S2-043: Batch create endpoint (partial success)](../S2-043/README.md)

**Наступна задача:** [S2-045: SlideVideoMapping migration](../S2-045/README.md)



---

## Детальний план реалізації

1. GET: list all mappings for node з validation_state
2. DELETE: видалити маппінг
3. Tenant isolation

---

## Очікуваний результат

Повний CRUD для маппінгів

---

## Тестування

### Автоматизовані тести

API tests: list, delete, tenant isolation

### Ручний контроль (Human testing)

GET маппінги для node → перевірити що validation_state видимий

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
