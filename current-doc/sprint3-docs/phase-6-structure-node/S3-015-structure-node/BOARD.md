# S3-015: StructureNode — рекурсивна output structure

**Тип:** New feature / Schema change
**Пріоритет:** Critical
**Складність:** XL
**Phase:** 6

## Опис

Замінити 4 фіксовані таблиці (Module/Lesson/Concept/Exercise) на 1 рекурсивну StructureNode з 25+ полями та `node_type` StrEnum. LLM output format не змінюється — конвертація на рівні persistence.

## Вплив

- Нова таблиця з 25+ полями (6 секцій)
- Видалення 4 таблиць + data migration
- Repositories, routes, schemas, tasks
- Тести (масивне оновлення)

## Definition of Done

- StructureNode створений з повним набором полів
- 4 старі таблиці видалені
- Generation pipeline + API працюють з новою структурою
