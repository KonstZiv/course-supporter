# S3-004: Видалити result fields з Job

**Тип:** Cleanup / Refactoring
**Пріоритет:** Medium
**Складність:** S
**Phase:** 1

## Опис

Видалити `result_material_id`, `result_snapshot_id` та CHECK constraint з Job. Результати знаходяться через "замовника" (MaterialEntry.pending_job_id, тощо).

## Вплив

- ORM, repositories, tasks, callbacks, schemas
- Alembic migration (DROP COLUMN x2 + DROP CONSTRAINT)

## Definition of Done

- Result fields видалені з ORM та API
- Migration працює в обох напрямках
