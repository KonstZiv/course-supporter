# S3-002: GET /nodes/tree повертає 500

**Тип:** Bug fix
**Пріоритет:** Critical (BLOCKER)
**Складність:** S
**Phase:** 0

## Опис

Tree endpoint повертає Internal Server Error. Ймовірна причина — lazy-loading рекурсивних ORM relationships при конвертації в Pydantic model.

## Вплив

- Неможливо побачити структуру курсу через API
- Блокує QA generation pipeline

## Definition of Done

- Tree endpoint повертає 200 з коректним JSON
- Працює для дерев з 3+ рівнями глибини
