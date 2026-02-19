# S2-045: SlideVideoMapping migration — Деталі для виконавця

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 2h

---

## Мета

Існуючі маппінги мігровані на нову структуру

## Контекст

Ця задача є частиною Epic "SlideVideoMapping — Redesign" (3-4 дні).
Загальна ціль epic: Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

## Залежності

**Попередня задача:** [S2-044: Mapping CRUD endpoints](../S2-044/README.md)

**Наступна задача:** [S2-046: Mapping validation unit tests](../S2-046/README.md)



---

## Детальний план реалізації

1. Створити нову таблицю (якщо не через ALTER)
2. Мігрувати дані: визначити presentation/video entry по context
3. validation_state = validated для існуючих (вже працюють)
4. Downgrade migration

---

## Очікуваний результат

Існуючі маппінги працюють в новій структурі

---

## Тестування

### Автоматизовані тести

Migration test: upgrade → data intact → downgrade → data intact

### Ручний контроль (Human testing)

Перевірити на staging що існуючі маппінги мігрували правильно

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
