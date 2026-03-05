# S3-003: Видалити content_fingerprint з MaterialEntry

**Тип:** Cleanup / Refactoring
**Пріоритет:** Medium
**Складність:** S
**Phase:** 1

## Опис

Видалити дублюючий field `content_fingerprint` з MaterialEntry. Використовувати існуючий `processed_hash` напряму в Merkle tree computation.

## Вплив

- ORM, schemas, ~55 тестів
- Alembic migration (DROP COLUMN)
- Нульовий ризик для production data

## Definition of Done

- Field видалений з ORM, API, тестів
- Fingerprint service використовує processed_hash
- Migration працює в обох напрямках
