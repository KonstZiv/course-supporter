# S3-019: Cascading Job Failure

**Тип:** Enhancement
**Пріоритет:** Medium
**Складність:** M
**Phase:** 9

## Опис

При failure Job → рекурсивно fail всі залежні jobs через `depends_on` JSONB. Зараз залежні jobs чекають нескінченно.

## Вплив

- JobRepository (нова логіка propagation)
- Task error handlers (інтеграція)

## Definition of Done

- Failure propagates рекурсивно
- Error message вказує на конкретну dependency
- Multi-level тести проходять
