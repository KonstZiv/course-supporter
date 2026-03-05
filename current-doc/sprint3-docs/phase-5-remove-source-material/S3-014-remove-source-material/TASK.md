# S3-014: Remove SourceMaterial

**Phase:** 5 (Remove SourceMaterial)
**Складність:** M
**Статус:** PENDING
**Залежність:** S3-013 (Course table dropped)

## Контекст

SourceMaterial — legacy таблиця (Sprint 0). В Sprint 2 (S2-060) додано dual-model detection:
- `arq_ingest_material` пробує `MaterialEntryRepository.get_by_id()` first
- Якщо None → fallback на `SourceMaterialRepository`
- `is_new_model` flag передається в `IngestionCallback`

Після видалення Course SourceMaterial більше немає сенсу — його FK `course_id` вже не існує.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/orm.py` | Видалити class SourceMaterial |
| `src/course_supporter/storage/source_material_repository.py` | ВИДАЛИТИ файл |
| `src/course_supporter/api/tasks.py` | Видалити SourceMaterial fallback, `is_new_model` logic |
| `src/course_supporter/ingestion/ingestion_callback.py` | Видалити `is_new_model` param, legacy branching |
| `src/course_supporter/api/routes/courses.py` | Видалити legacy upload endpoint (якщо ще існує) |
| `src/course_supporter/api/schemas.py` | Видалити SourceMaterial schemas |
| `src/course_supporter/models/source.py` | Видалити SourceMaterial Pydantic model |
| `migrations/versions/` | DROP TABLE source_materials |
| `tests/` | Видалити SourceMaterial тести, спростити task/callback тести |

## Деталі реалізації

### 1. Simplify arq_ingest_material (tasks.py)

```python
# Було:
entry = await entry_repo.get_by_id(material_id)
if entry is not None:
    is_new_model = True
    # ...
else:
    sm = await sm_repo.get(material_id)
    is_new_model = False
    # ...

# Стало:
entry = await entry_repo.get_by_id(material_id)
if entry is None:
    raise ValueError(f"MaterialEntry not found: {material_id}")
# ... simple path only
```

### 2. Simplify IngestionCallback

```python
# Було:
class IngestionCallback:
    async def on_success(self, is_new_model: bool):
        if is_new_model:
            await entry_repo.complete_processing(...)
        else:
            await sm_repo.update_status(...)

# Стало:
class IngestionCallback:
    async def on_success(self):
        await entry_repo.complete_processing(...)
```

### 3. Migration

```python
def upgrade():
    op.drop_table("source_materials")

def downgrade():
    # Recreate table structure (without data)
    op.create_table("source_materials", ...)
```

## Acceptance Criteria

- [ ] SourceMaterial ORM видалений
- [ ] SourceMaterialRepository видалений
- [ ] `is_new_model` branching видалений з tasks/callback
- [ ] `arq_ingest_material` працює тільки з MaterialEntry
- [ ] Migration: DROP TABLE source_materials
- [ ] Всі тести проходять (спрощені)
