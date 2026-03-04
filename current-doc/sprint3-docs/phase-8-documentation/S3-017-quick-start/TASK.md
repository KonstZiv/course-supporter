# S3-017: Quick Start Guide

**Phase:** 8 (Documentation)
**Складність:** S
**Статус:** PENDING
**Залежність:** S3-016 (final schema + URL patterns)

## Контекст

Продовження S2-058 (Quick Start). Повне переписування для нових URL patterns `/nodes/...`.

## Deliverable

`docs/api/quick-start.md` — покрокова інструкція з curl прикладами:

1. Створити root node (= курс)
2. Додати child nodes (теми/модулі)
3. Завантажити матеріали (file upload + URL)
4. Перевірити статус ingestion (Jobs)
5. Запустити generation
6. Отримати результат (StructureNode tree)
7. SlideVideoMapping (optional)

## Файли

| Файл | Зміни |
|------|-------|
| `docs/api/quick-start.md` | Повне переписування |
| `docs/api/flow-guide.md` | Оновити URLs та field names |

## Acceptance Criteria

- [ ] Curl приклади працюють на production
- [ ] Покриває повний flow від створення до отримання результату
- [ ] Мова: українська
- [ ] Deployed на GitHub Pages
