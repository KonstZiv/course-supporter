# S2-046: Mapping validation unit tests — Деталі для виконавця

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 4h

---

## Мета

Повне тестове покриття для валідації маппінгів

## Контекст

Ця задача є частиною Epic "SlideVideoMapping — Redesign" (3-4 дні).
Загальна ціль epic: Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

## Залежності

**Попередня задача:** [S2-045: SlideVideoMapping migration](../S2-045/README.md)



---

## Детальний план реалізації

1. Level 1: wrong type, wrong node, invalid timecode
2. Level 2: slide range, timecode range, boundary values
3. Level 3: pending→validated lifecycle, pending→error lifecycle
4. Batch: partial success scenarios
5. Auto-revalidation: ingestion triggers

---

## Очікуваний результат

Повне покриття всіх validation scenarios

---

## Тестування

### Автоматизовані тести

pytest

### Ручний контроль (Human testing)

Review — чи покриті edge cases з AR-7

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
