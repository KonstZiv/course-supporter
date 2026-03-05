# S3-004: Remove result_material_id / result_snapshot_id from Job

**Phase:** 1 (Cleanup)
**Складність:** S
**Статус:** PENDING

## Контекст

`Job.result_material_id` та `Job.result_snapshot_id` — зворотні посилання на результати Job. Вони дублюють зв'язки що вже існують на стороні результату (`MaterialEntry.pending_job_id`, `StructureSnapshot` через `node_id + timestamp`). Кожен новий тип Job потребував би нового result field + CHECK constraint.

Рішення (зафіксоване в `current-doc/backlog.md`): видалити обидва поля та CHECK constraint `chk_job_result_exclusive`.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/orm.py` | Видалити `result_material_id`, `result_snapshot_id` з Job |
| `src/course_supporter/storage/job_repository.py` | Видалити методи/логіку що встановлює result fields |
| `src/course_supporter/api/tasks.py` | Видалити код що записує result_material_id/result_snapshot_id |
| `src/course_supporter/ingestion/ingestion_callback.py` | Видалити result assignment |
| `src/course_supporter/api/schemas.py` | Видалити з JobResponse |
| `migrations/versions/` | DROP COLUMN x2 + DROP CONSTRAINT |
| `tests/` | ~4 файли з тестами |

## Деталі реалізації

### 1. ORM (orm.py)

Видалити з класу Job:
```python
result_material_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("material_entries.id"), nullable=True)
result_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("course_structure_snapshots.id"), nullable=True)
```

Видалити CHECK constraint з `__table_args__`:
```python
CheckConstraint(
    "(result_material_id IS NULL) OR (result_snapshot_id IS NULL)",
    name="chk_job_result_exclusive",
)
```

### 2. JobRepository

Знайти місця де встановлюються `result_material_id` / `result_snapshot_id` — видалити.

### 3. Tasks / Callback

В `arq_ingest_material` та `ingestion_callback.py` — видалити код що записує result:
```python
# Видалити:
job.result_material_id = entry.id
```

### 4. Migration

```python
def upgrade():
    op.drop_constraint("chk_job_result_exclusive", "jobs")
    op.drop_column("jobs", "result_material_id")
    op.drop_column("jobs", "result_snapshot_id")
```

## Acceptance Criteria

- [ ] `result_material_id` та `result_snapshot_id` відсутні в ORM
- [ ] CHECK constraint `chk_job_result_exclusive` видалений
- [ ] Всі задачі/callbacks не пишуть в ці поля
- [ ] Alembic migration працює
- [ ] Всі тести проходять
