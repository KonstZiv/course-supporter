# S3-012: Переписати routes та repositories (Course → Node)

**Тип:** Breaking change / Refactoring
**Пріоритет:** Critical
**Складність:** XL
**Phase:** 4b

## Опис

Переписати ВСІ routes та repositories для роботи без Course entity. URL змінюються з `/courses/{id}/...` на `/nodes/{id}/...`. Root MaterialNode = курс.

## Вплив

- **ВСІ** route файли
- **ВСІ** repositories
- Enqueue, orchestrator, tasks
- Більшість тестів
- **Breaking API change**

## Definition of Done

- Нові URL patterns працюють
- Course entity не використовується
- Всі тести проходять
