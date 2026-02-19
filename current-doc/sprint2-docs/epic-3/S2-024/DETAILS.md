# S2-024: FingerprintService — material level — Деталі для виконавця

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 2h

---

## Мета

Lazy cached sha256 від processed_content

## Контекст

Ця задача є частиною Epic "Merkle Fingerprints" (2-3 дні).
Загальна ціль epic: Lazy cached fingerprints з каскадною інвалідацією знизу вгору.

## Залежності

**Наступна задача:** [S2-025: FingerprintService — node level (recursive)](../S2-025/README.md)



---

## Детальний план реалізації

1. ensure_material_fp(entry) → sha256(processed_content)
2. Якщо content_fingerprint не None → повертає кешоване
3. flush після розрахунку

---

## Очікуваний результат

content_fingerprint розраховується лише раз до наступної інвалідації

---

## Тестування

### Автоматизовані тести

Unit test: calculate, cache hit, invalidation → recalculate

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
