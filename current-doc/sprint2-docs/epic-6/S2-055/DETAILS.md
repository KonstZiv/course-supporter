# S2-055: Mapping warnings in generation — Деталі для виконавця

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 2h

---

## Мета

Маппінги з pending_validation/validation_failed включаються як warnings

## Контекст

Ця задача є частиною Epic "Structure Generation Pipeline" (3-4 дні).
Загальна ціль epic: Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

## Залежності

**Попередня задача:** [S2-054: MergeStep refactor — tree-aware](../S2-054/README.md)

**Наступна задача:** [S2-056: Structure generation tests](../S2-056/README.md)



---

## Детальний план реалізації

1. Зібрати маппінги в піддереві
2. Якщо є pending_validation або validation_failed → warnings в response
3. Не блокує generation, лише інформує

---

## Очікуваний результат

Generation response включає warnings про problematic маппінги

---

## Тестування

### Автоматизовані тести

Unit test: generate з pending mappings → warnings in response

### Ручний контроль (Human testing)

Перевірити що warnings зрозумілі і допомагають виправити проблему

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
