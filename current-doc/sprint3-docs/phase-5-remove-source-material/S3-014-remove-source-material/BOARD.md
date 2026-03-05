# S3-014: Видалити SourceMaterial

**Тип:** Cleanup / Schema change
**Пріоритет:** High
**Складність:** M
**Phase:** 5

## Опис

Видалити legacy SourceMaterial table та dual-model branching code. Ingestion працює тільки через MaterialEntry.

## Вплив

- ORM, repository (видалення)
- Tasks/callback (спрощення)
- Migration (DROP TABLE)

## Definition of Done

- SourceMaterial повністю видалений
- Ingestion спрощений до single-model path
