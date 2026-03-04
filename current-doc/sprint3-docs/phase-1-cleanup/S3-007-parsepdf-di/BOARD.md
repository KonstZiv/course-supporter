# S3-007: ParsePDFFunc DI Extraction

**Тип:** Refactoring
**Пріоритет:** Low
**Складність:** S
**Phase:** 1

## Опис

Витягти PDF parsing з inline коду PresentationProcessor в окрему функцію з DI через factory — як всі інші heavy steps (transcribe, describe_slides, scrape_web).

## Вплив

- PresentationProcessor (DI через __init__)
- Factory (нова залежність)
- Нові тести

## Definition of Done

- `local_parse_pdf()` як окрема функція
- DI через factory pattern
- Тести покривають extraction
