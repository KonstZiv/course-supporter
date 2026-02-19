# S2-051: Cascade generation orchestrator — Деталі для виконавця

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 4h

---

## Мета

Автоматично запускає ingestion для stale materials перед generation

## Контекст

Ця задача є частиною Epic "Structure Generation Pipeline" (3-4 дні).
Загальна ціль epic: Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

## Залежності

**Попередня задача:** [S2-050: Generate structure ARQ task](../S2-050/README.md)

**Наступна задача:** [S2-052: Free vs Guided mode](../S2-052/README.md)



---

## Детальний план реалізації

1. find_stale_materials(node_id)
2. Якщо є stale: enqueue ingestion jobs для кожного
3. enqueue structure generation з depends_on = ingestion job ids
4. Якщо всі READY: check fingerprint → idempotency або enqueue generation
5. Return 200 (existing) або 202 (new jobs) з планом і estimate

---

## Очікуваний результат

Один POST trigger каскадно обробляє все піддерево

---

## Тестування

### Автоматизовані тести

Integration test: tree з mix READY/RAW → ingestion jobs + generation job created з depends_on

### Ручний контроль (Human testing)

POST generate для node з RAW матеріалами → перевірити response plan: ingestion_required list, estimate

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
