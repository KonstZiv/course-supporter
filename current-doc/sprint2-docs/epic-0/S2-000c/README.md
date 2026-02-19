# S2-000c: ERD page — Mermaid rendering

**Epic:** EPIC-0 — Project Documentation Infrastructure
**Оцінка:** 2h

---

## Мета

ERD діаграма рендериться на docs site як інтерактивна Mermaid-схема

## Що робимо

Додати ERD v4 в docs/architecture/erd.md, налаштувати mermaid plugin

## Як робимо

1. Переконатись що mkdocs-mermaid2-plugin встановлений і в mkdocs.yml
2. Створити docs/architecture/erd.md з поточною ERD (Sprint-2-ERD.mermaid)
3. Обгорнути Mermaid-код в ```mermaid блок
4. Перевірити рендеринг локально

## Очікуваний результат

ERD сторінка відображає повну діаграму з усіма таблицями і зв'язками

## Як тестуємо

**Автоматизовано:** mkdocs build не падає на mermaid-блоках

**Human control:** Відкрити ERD сторінку, перевірити що всі таблиці видимі, зв'язки правильні, діаграма інтерактивна (zoom/pan)

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
