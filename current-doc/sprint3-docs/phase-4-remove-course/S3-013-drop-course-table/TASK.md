# S3-013: Drop Course Table

**Phase:** 4c (Remove Course — Drop)
**Складність:** S
**Статус:** PENDING
**Залежність:** S3-012

## Контекст

Після того як всі references на Course видалені (S3-012), можна безпечно видалити саму таблицю та ORM.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/orm.py` | Видалити class Course |
| `src/course_supporter/storage/course_repository.py` | Видалити файл (якщо не видалений в S3-012) |
| `src/course_supporter/api/schemas.py` | Видалити CourseCreate, CourseResponse, тощо |
| `migrations/versions/` | DROP TABLE courses |
| `tests/` | Видалити Course fixtures та тести |

## Migration

```python
def upgrade():
    # First drop all FKs pointing to courses
    op.drop_constraint("fk_material_nodes_course", "material_nodes", type_="foreignkey")
    # ... other FK drops if any remain

    # Drop column
    op.drop_column("material_nodes", "course_id")

    # Drop table
    op.drop_table("courses")
```

**Важливо:** Перевірити що жодна таблиця не має FK на courses перед DROP.

## Acceptance Criteria

- [ ] Course ORM видалений
- [ ] CourseRepository видалений
- [ ] Course schemas видалені
- [ ] `material_nodes.course_id` видалений
- [ ] Migration: DROP TABLE courses
- [ ] Всі тести проходять
