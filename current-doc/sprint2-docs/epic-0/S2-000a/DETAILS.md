# S2-000a: mkdocs setup + theme — Деталі для виконавця

**Epic:** EPIC-0 — Project Documentation Infrastructure
**Оцінка:** 2h

---

## Мета

Базовий mkdocs проєкт з material theme, готовий до локальної розробки

## Контекст

Ця задача є частиною Epic "Project Documentation Infrastructure" (1-2 дні).
Загальна ціль epic: Документація проєкту на GitHub Pages (mkdocs). ERD що оновлюється, структуровані описи спрінтів.

## Залежності

**Наступна задача:** [S2-000b: GitHub Actions → GitHub Pages deploy](../S2-000b/README.md)



---

## Детальний план реалізації

1. Додати mkdocs-material і mkdocs-mermaid2-plugin в [project.optional-dependencies].docs
2. Створити mkdocs.yml з базовою конфігурацією (theme, plugins, nav)
3. Створити docs/ структуру з index.md
4. Перевірити mkdocs serve локально

---

## Очікуваний результат

mkdocs serve показує landing page з навігацією

---

## Тестування

### Автоматизовані тести

`mkdocs build --strict` проходить без помилок

### Ручний контроль (Human testing)

Відкрити localhost:8000, перевірити що сторінка відображається, навігація працює, тема material застосована

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
