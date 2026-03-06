# S3-020d: Selective refine pass

**Тип:** New feature
**Пріоритет:** Medium
**Складність:** M
**Phase:** 10

## Опис

Після правок користувача в structure_nodes — перегенерація лише зміненого вузла + предків зі збереженням ручних правок. Ковзне вікно використовується для контексту.

## Вплив

- RefineAgent (новий)
- Промпти для refine (нові)
- Orchestrator (trigger_refine)
- API endpoint POST /nodes/{node_id}/refine (новий)

## Definition of Done

- RefineAgent зберігає ручні правки
- Перегенерація лише зміненого вузла + предків
- Sibling'и як контекст, не перегенеровуються
- API endpoint працює
