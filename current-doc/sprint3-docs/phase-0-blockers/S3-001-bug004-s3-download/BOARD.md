# S3-001: S3 download не працює для B2 матеріалів

**Тип:** Bug fix
**Пріоритет:** Critical (BLOCKER)
**Складність:** S
**Phase:** 0

## Опис

S3 client не має правильної конфігурації для Backblaze B2 (SigV4 + path-style addressing). Всі матеріали завантажені через file upload (presentations, texts, PPTX) не обробляються.

## Вплив

- 17 з 28 матеріалів на production не оброблені
- Блокує QA Sprint 2
- Блокує початок Sprint 3

## Definition of Done

- B2 матеріали обробляються на production
- Нові тести покривають S3 конфігурацію та error handling
