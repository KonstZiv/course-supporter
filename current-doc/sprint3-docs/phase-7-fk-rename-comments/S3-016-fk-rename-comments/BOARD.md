# S3-016: FK Rename + DB Comments

**Тип:** Refactoring / Documentation
**Пріоритет:** Medium
**Складність:** L
**Phase:** 7

## Опис

Перейменувати всі FK на `{tablename}_id` конвенцію. Додати PostgreSQL COMMENT ON для всіх таблиць та non-obvious колонок.

## Вплив

- ВСІ файли з references на FK names
- Масивний але механічний search-replace
- Migration (ALTER COLUMN + COMMENT ON)

## Definition of Done

- Всі FK з конвенцією `{tablename}_id`
- DB self-documenting через comments
