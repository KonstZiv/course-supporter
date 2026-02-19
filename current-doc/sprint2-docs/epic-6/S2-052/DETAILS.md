# S2-052: Free vs Guided mode — Деталі для виконавця

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 3h

---

## Мета

Два режими генерації з різними промптами

## Контекст

Ця задача є частиною Epic "Structure Generation Pipeline" (3-4 дні).
Загальна ціль epic: Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

## Залежності

**Попередня задача:** [S2-051: Cascade generation orchestrator](../S2-051/README.md)

**Наступна задача:** [S2-053: Structure generation API](../S2-053/README.md)



---

## Детальний план реалізації

1. mode='free': prompt дозволяє вільну структуру
2. mode='guided': prompt містить input tree як constraint
3. Зберігається в snapshot.mode
4. Idempotency per (fingerprint + mode)

---

## Очікуваний результат

Free і guided генерують різні структури з одних матеріалів

---

## Тестування

### Автоматизовані тести

Unit test: різні prompts для різних modes, idempotency per mode

### Ручний контроль (Human testing)

Згенерувати free і guided для одного курсу → порівняти результати — guided зберігає input structure

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
