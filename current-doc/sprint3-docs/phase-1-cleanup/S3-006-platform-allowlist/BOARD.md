# S3-006: Platform Allowlist для web sources

**Тип:** Enhancement
**Пріоритет:** Medium
**Складність:** S
**Phase:** 1

## Опис

Визначити allowlist підтверджених платформ для video та web source types. Попереджувати (не блокувати) при невідомих URL.

## Вплив

- Config system (новий YAML)
- Upload validation (warning message)

## Definition of Done

- Allowlist в config з verified платформами
- Warning для невідомих URL в API response
