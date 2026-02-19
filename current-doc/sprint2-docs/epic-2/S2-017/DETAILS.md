# S2-017: MaterialEntry repository — Деталі для виконавця

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 4h

---

## Мета

CRUD для MaterialEntry з pending receipt management і hash invalidation

## Контекст

Ця задача є частиною Epic "MaterialTree + MaterialEntry" (4-5 днів).
Загальна ціль epic: Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

## Залежності

**Попередня задача:** [S2-016: MaterialNode repository](../S2-016/README.md)

**Наступна задача:** [S2-018: Alembic migration: new tables + data migration](../S2-018/README.md)



---

## Детальний план реалізації

1. create(node_id, source_type, source_url, filename)
2. set_pending(entry_id, job_id) → pending_job_id + pending_since
3. complete_processing(entry_id, processed_content, processed_hash)
4. fail_processing(entry_id, error_message)
5. update_source(entry_id, new_url) → raw_hash=None (invalidation)
6. ensure_raw_hash(entry) → lazy calculation

---

## Очікуваний результат

Повний CRUD з lifecycle management

---

## Тестування

### Автоматизовані тести

Unit tests: full lifecycle RAW→PENDING→READY, hash invalidation, ensure_raw_hash

### Ручний контроль (Human testing)

Немає (покривається unit tests + Epic 1 integration)

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
