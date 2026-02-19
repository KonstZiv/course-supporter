# S2-029: Fingerprint в API responses — Деталі для виконавця

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 2h

---

## Мета

Всі GET endpoints повертають fingerprints

## Контекст

Ця задача є частиною Epic "Merkle Fingerprints" (2-3 дні).
Загальна ціль epic: Lazy cached fingerprints з каскадною інвалідацією знизу вгору.

## Залежності

**Попередня задача:** [S2-028: Integration з MaterialEntry/Node repositories](../S2-028/README.md)

**Наступна задача:** [S2-030: Fingerprint unit tests](../S2-030/README.md)



---

## Детальний план реалізації

1. CourseDetailResponse: course_fingerprint
2. NodeResponse: node_fingerprint
3. MaterialEntryResponse: content_fingerprint
4. fingerprint=null означає 'потребує перерахунку'

---

## Очікуваний результат

API response містить fingerprints на всіх рівнях

---

## Тестування

### Автоматизовані тести

API test: GET course → verify fingerprints present and correct

### Ручний контроль (Human testing)

GET /courses/{id} — перевірити що fingerprints видимі на кожному рівні дерева

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
