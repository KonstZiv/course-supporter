# S2-030: Fingerprint unit tests — Деталі для виконавця

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 3h

---

## Мета

Повне тестове покриття Merkle fingerprints

## Контекст

Ця задача є частиною Epic "Merkle Fingerprints" (2-3 дні).
Загальна ціль epic: Lazy cached fingerprints з каскадною інвалідацією знизу вгору.

## Залежності

**Попередня задача:** [S2-029: Fingerprint в API responses](../S2-029/README.md)



---

## Детальний план реалізації

1. Merkle correctness: known inputs → known hash
2. Cascade invalidation: change deep leaf → verify path to root
3. Independence: change in branch A doesn't affect branch B
4. Lazy calculation: ensure only calculates when needed
5. Edge: empty node, single material, very deep tree

---

## Очікуваний результат

Всі fingerprint тести зелені

---

## Тестування

### Автоматизовані тести

pytest

### Ручний контроль (Human testing)

Review — чи покриті всі сценарії з AR-4

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
