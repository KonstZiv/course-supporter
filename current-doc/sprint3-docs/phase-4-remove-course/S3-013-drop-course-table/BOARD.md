# S3-013: Видалити Course table

**Тип:** Schema change (destructive)
**Пріоритет:** High
**Складність:** S
**Phase:** 4c

## Опис

Фінальний крок видалення Course entity — DROP TABLE після того як всі references перенесені на MaterialNode.

## Вплив

- ORM, schemas (видалення)
- Migration (DROP TABLE)
- Необоротна зміна (downgrade потребує data restoration)

## Definition of Done

- Course table видалена з БД
- Жодних references на Course в коді
