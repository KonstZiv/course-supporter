# S2-028: Integration з MaterialEntry/Node repositories — Деталі для виконавця

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 2h

---

## Мета

Auto-invalidation при будь-яких CRUD операціях

## Контекст

Ця задача є частиною Epic "Merkle Fingerprints" (2-3 дні).
Загальна ціль epic: Lazy cached fingerprints з каскадною інвалідацією знизу вгору.

## Залежності

**Попередня задача:** [S2-027: Cascade invalidation (_invalidate_up)](../S2-027/README.md)

**Наступна задача:** [S2-029: Fingerprint в API responses](../S2-029/README.md)



---

## Детальний план реалізації

1. MaterialEntryRepository.update_source → invalidate entry fp + _invalidate_up
2. MaterialEntryRepository.complete_processing → invalidate entry fp + _invalidate_up
3. MaterialNodeRepository.move → invalidate old parent + new parent
4. MaterialNodeRepository.delete → invalidate parent

---

## Очікуваний результат

Fingerprints автоматично інвалідуються при будь-яких змінах

---

## Тестування

### Автоматизовані тести

Integration tests: CRUD operations → verify fingerprint invalidation

### Ручний контроль (Human testing)

Немає

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
