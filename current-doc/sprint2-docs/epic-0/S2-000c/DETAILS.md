# S2-000c: ERD page — Mermaid rendering — Деталі для виконавця

**Epic:** EPIC-0 — Project Documentation Infrastructure
**Оцінка:** 2h

---

## Мета

ERD діаграма рендериться на docs site як інтерактивна Mermaid-схема

## Контекст

Ця задача є частиною Epic "Project Documentation Infrastructure" (1-2 дні).
Загальна ціль epic: Документація проєкту на GitHub Pages (mkdocs). ERD що оновлюється, структуровані описи спрінтів.

## Залежності

**Попередня задача:** [S2-000b: GitHub Actions → GitHub Pages deploy](../S2-000b/README.md)

**Наступна задача:** [S2-000d: Sprint 1 — ретроспективний опис](../S2-000d/README.md)



---

## Детальний план реалізації

1. Переконатись що mkdocs-mermaid2-plugin встановлений і в mkdocs.yml
2. Створити docs/architecture/erd.md з поточною ERD (Sprint-2-ERD.mermaid)
3. Обгорнути Mermaid-код в ```mermaid блок
4. Перевірити рендеринг локально

---

## Очікуваний результат

ERD сторінка відображає повну діаграму з усіма таблицями і зв'язками

---

## Тестування

### Автоматизовані тести

mkdocs build не падає на mermaid-блоках

### Ручний контроль (Human testing)

Відкрити ERD сторінку, перевірити що всі таблиці видимі, зв'язки правильні, діаграма інтерактивна (zoom/pan)

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
