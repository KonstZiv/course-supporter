# S2-000f: Структура документації + landing — Деталі для виконавця

**Epic:** EPIC-0 — Project Documentation Infrastructure
**Оцінка:** 2h

---

## Мета

Повна навігаційна структура docs site

## Контекст

Ця задача є частиною Epic "Project Documentation Infrastructure" (1-2 дні).
Загальна ціль epic: Документація проєкту на GitHub Pages (mkdocs). ERD що оновлюється, структуровані описи спрінтів.

## Залежності

**Попередня задача:** [S2-000e: Sprint 2 — поточний опис](../S2-000e/README.md)

**Наступна задача:** [S2-000g: README оновлення](../S2-000g/README.md)



---

## Детальний план реалізації

1. Створити docs/index.md (landing — overview проєкту)
2. Створити placeholder-и для всіх розділів
3. Налаштувати nav в mkdocs.yml
4. Додати architecture/decisions.md (ADR format)

---

## Очікуваний результат

Docs site має повну навігаційну структуру, placeholder-и для майбутнього контенту

---

## Тестування

### Автоматизовані тести

mkdocs build --strict (перевірка broken links)

### Ручний контроль (Human testing)

Пройти по всій навігації — структура логічна, placeholder-и зрозумілі

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
