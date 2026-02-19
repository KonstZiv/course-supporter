# S2-027: Cascade invalidation (_invalidate_up) — Деталі для виконавця

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 2h

---

## Мета

Зміна матеріалу → інвалідація fingerprints від точки зміни до кореня

## Контекст

Ця задача є частиною Epic "Merkle Fingerprints" (2-3 дні).
Загальна ціль epic: Lazy cached fingerprints з каскадною інвалідацією знизу вгору.

## Залежності

**Попередня задача:** [S2-026: FingerprintService — course level](../S2-026/README.md)

**Наступна задача:** [S2-028: Integration з MaterialEntry/Node repositories](../S2-028/README.md)



---

## Детальний план реалізації

1. While node is not None: node.node_fingerprint = None; node = node.parent
2. flush після циклу
3. Інтегрувати в MaterialEntry модифікації (auto-invalidation)

---

## Очікуваний результат

Зміна leaf → всі ancestor nodes мають fingerprint=None

---

## Тестування

### Автоматизовані тести

Unit test: change leaf material → verify all ancestors invalidated, siblings untouched

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
