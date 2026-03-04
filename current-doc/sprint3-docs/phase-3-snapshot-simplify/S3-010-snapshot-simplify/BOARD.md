# S3-010: Спрощення StructureSnapshot

**Тип:** Refactoring
**Пріоритет:** High
**Складність:** M
**Phase:** 3

## Опис

Видалити дубльовані поля метаданих LLM з Snapshot. Замінити на FK → ExternalServiceCall як єдине джерело правди. 6 полів замість 12.

## Вплив

- ORM, repository, tasks, routes, schemas
- Data migration (production snapshots)
- Generation pipeline

## Definition of Done

- Snapshot має 6 полів з FK на ExternalServiceCall
- Existing data migrated
- Generation pipeline + API працюють
