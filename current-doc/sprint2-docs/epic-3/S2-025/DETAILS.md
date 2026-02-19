# S2-025: FingerprintService — node level (recursive) — Деталі для виконавця

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 3h

---

## Мета

Merkle hash для вузла: hash(material_fps + child_node_fps)

## Контекст

Ця задача є частиною Epic "Merkle Fingerprints" (2-3 дні).
Загальна ціль epic: Lazy cached fingerprints з каскадною інвалідацією знизу вгору.

## Залежності

**Попередня задача:** [S2-024: FingerprintService — material level](../S2-024/README.md)

**Наступна задача:** [S2-026: FingerprintService — course level](../S2-026/README.md)



---

## Детальний план реалізації

1. ensure_node_fp(node): sorted materials fps ('m:...') + sorted children fps ('n:...')
2. sha256 від joined parts
3. Рекурсія вниз по дереву
4. Кешування на кожному рівні

---

## Очікуваний результат

node_fingerprint = Merkle hash всього піддерева

---

## Тестування

### Автоматизовані тести

Unit tests: single node, nested 3 levels, deterministic (same data = same hash), empty node

### Ручний контроль (Human testing)

Немає (покривається unit tests)

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
