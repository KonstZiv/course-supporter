# S3-008: LLMCall → ExternalServiceCall

**Тип:** Refactoring
**Пріоритет:** High
**Складність:** M
**Phase:** 2

## Опис

Перейменувати таблицю `llm_calls` → `external_service_calls` з оновленням полів для universal billing (unit_type, unit_in, unit_out) та зв'язком з Job.

## Вплив

- ORM, repository, routes, schemas, тести
- Alembic migration (rename + alter + add)
- Блокує Phase 3 (Snapshot FK)

## Definition of Done

- Таблиця перейменована з 16 полями
- Всі references оновлені
- Migration працює в обох напрямках
