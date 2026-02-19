# S2-026: FingerprintService — course level — Деталі для виконавця

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 1h

---

## Мета

Course fingerprint = hash від root nodes

## Контекст

Ця задача є частиною Epic "Merkle Fingerprints" (2-3 дні).
Загальна ціль epic: Lazy cached fingerprints з каскадною інвалідацією знизу вгору.

## Залежності

**Попередня задача:** [S2-025: FingerprintService — node level (recursive)](../S2-025/README.md)

**Наступна задача:** [S2-027: Cascade invalidation (_invalidate_up)](../S2-027/README.md)



---

## Детальний план реалізації

1. Get root nodes (parent_id=None) для course
2. sorted ensure_node_fp для кожного
3. sha256 від joined parts

---

## Очікуваний результат

Єдиний hash що представляє стан всього дерева курсу

---

## Тестування

### Автоматизовані тести

Unit test: course fp changes when any material changes, stable when nothing changes

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
