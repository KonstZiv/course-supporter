# S2-050: Generate structure ARQ task — Деталі для виконавця

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 4h

---

## Мета

ARQ task що виконує merge → ArchitectAgent → save snapshot

## Контекст

Ця задача є частиною Epic "Structure Generation Pipeline" (3-4 дні).
Загальна ціль epic: Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

## Залежності

**Попередня задача:** [S2-049: Conflict detection (subtree overlap)](../S2-049/README.md)

**Наступна задача:** [S2-051: Cascade generation orchestrator](../S2-051/README.md)



---

## Детальний план реалізації

1. Collect READY materials from subtree
2. MergeStep → CourseContext
3. ArchitectAgent.generate(context, mode) → CourseStructure
4. Save CourseStructureSnapshot з fingerprint
5. Handle errors gracefully

---

## Очікуваний результат

Generation task створює snapshot і зберігає в DB

---

## Тестування

### Автоматизовані тести

Integration test з mock ArchitectAgent: task → snapshot created з правильним fingerprint

### Ручний контроль (Human testing)

Запустити generation → перевірити snapshot в DB: structure JSON, fingerprint, LLM metadata

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
