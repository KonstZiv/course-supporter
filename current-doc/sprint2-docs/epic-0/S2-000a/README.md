# S2-000a: mkdocs setup + theme

**Epic:** EPIC-0 — Project Documentation Infrastructure
**Оцінка:** 2h

---

## Мета

Базовий mkdocs проєкт з material theme, готовий до локальної розробки

## Що робимо

Створити mkdocs.yml, встановити mkdocs-material, налаштувати nav, інтегрувати в pyproject.toml

## Як робимо

1. Додати mkdocs-material і mkdocs-mermaid2-plugin в [project.optional-dependencies].docs
2. Створити mkdocs.yml з базовою конфігурацією (theme, plugins, nav)
3. Створити docs/ структуру з index.md
4. Перевірити mkdocs serve локально

## Очікуваний результат

mkdocs serve показує landing page з навігацією

## Як тестуємо

**Автоматизовано:** `mkdocs build --strict` проходить без помилок

**Human control:** Відкрити localhost:8000, перевірити що сторінка відображається, навігація працює, тема material застосована

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
