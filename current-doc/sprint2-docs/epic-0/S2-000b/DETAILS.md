# S2-000b: GitHub Actions → GitHub Pages deploy — Деталі для виконавця

**Epic:** EPIC-0 — Project Documentation Infrastructure
**Оцінка:** 2h

---

## Мета

Автоматичний deploy документації при push в main

## Контекст

Ця задача є частиною Epic "Project Documentation Infrastructure" (1-2 дні).
Загальна ціль epic: Документація проєкту на GitHub Pages (mkdocs). ERD що оновлюється, структуровані описи спрінтів.

## Залежності

**Попередня задача:** [S2-000a: mkdocs setup + theme](../S2-000a/README.md)

**Наступна задача:** [S2-000c: ERD page — Mermaid rendering](../S2-000c/README.md)



---

## Детальний план реалізації

1. Створити .github/workflows/docs.yml
2. Workflow: checkout → setup python → install deps → mkdocs gh-deploy
3. Увімкнути GitHub Pages в Settings → Pages → Source: gh-pages branch
4. Перевірити що push в main тригерить deploy

---

## Очікуваний результат

Push в main → через 1-2 хв docs site оновлюється на GitHub Pages

---

## Тестування

### Автоматизовані тести

GitHub Actions workflow green

### Ручний контроль (Human testing)

Зробити commit в main, через 2 хв відкрити GitHub Pages URL, перевірити що зміни з'явились

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
