# S3-011: Розширити MaterialNode для ролі "курсу"

**Тип:** Schema change (additive)
**Пріоритет:** High
**Складність:** M
**Phase:** 4a

## Опис

Додати `tenant_id`, `learning_goal`, `expected_knowledge`, `expected_skills` на MaterialNode. Data migration з Course table. Безпечний deploy — нічого не ламає.

## Вплив

- ORM (4 нові колонки)
- Data migration (copy tenant_id з Course)
- Schemas (нові поля в response)

## Definition of Done

- MaterialNode має 4 нові поля
- Existing data migrated
- Additive — backward compatible
