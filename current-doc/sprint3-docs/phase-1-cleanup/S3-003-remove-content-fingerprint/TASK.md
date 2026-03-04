# S3-003: Remove content_fingerprint from MaterialEntry

**Phase:** 1 (Cleanup)
**Складність:** S
**Статус:** PENDING

## Контекст

`MaterialEntry.content_fingerprint` — це кешований `sha256(processed_content)`, проміжний крок для обчислення `MaterialNode.node_fingerprint`. Але `processed_hash` — це також `sha256` вмісту. Два поля дублюють одне одного.

Рішення (зафіксоване в `current-doc/backlog.md`): видалити `content_fingerprint`, використовувати `processed_hash` напряму в Merkle tree.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/orm.py` | Видалити `content_fingerprint` column з MaterialEntry |
| `src/course_supporter/storage/fingerprint.py` | `_compute_material_fp()` → повертати `entry.processed_hash` напряму |
| `src/course_supporter/storage/material_entry_repository.py` | Видалити references до content_fingerprint |
| `src/course_supporter/api/schemas.py` | Видалити content_fingerprint з response schemas |
| `migrations/versions/` | Нова міграція: `DROP COLUMN content_fingerprint` |
| `tests/` | Оновити ~55 тестів що references content_fingerprint |

## Деталі реалізації

### 1. ORM (orm.py)

Видалити:
```python
content_fingerprint: Mapped[str | None] = mapped_column(String(64))
```

### 2. FingerprintService (fingerprint.py)

В `_compute_material_fp()` замінити:
```python
# Було:
fp = sha256(entry.processed_content.encode()).hexdigest()
entry.content_fingerprint = fp
return fp

# Стало:
return entry.processed_hash  # вже sha256, обчислений при ingestion
```

### 3. Merkle hash computation

Перевірити `_compute_node_fp()` — формула:
```
node_fingerprint = sha256(sorted(
    ["m:" + entry.processed_hash for ...]
    + ["n:" + child.node_fingerprint for ...]
))
```
Якщо вже використовує `processed_hash` — нічого міняти. Якщо використовує `content_fingerprint` — замінити.

### 4. Migration

```python
def upgrade():
    op.drop_column("material_entries", "content_fingerprint")

def downgrade():
    op.add_column("material_entries",
        sa.Column("content_fingerprint", sa.String(64), nullable=True))
```

### 5. Тести

Grep по `content_fingerprint` в tests/ — видалити або замінити на `processed_hash` де потрібно.

## Acceptance Criteria

- [ ] `content_fingerprint` відсутній в ORM, schemas, API responses
- [ ] `FingerprintService` використовує `processed_hash` напряму
- [ ] Merkle hash обчислюється коректно (тести fingerprint service)
- [ ] Alembic migration проходить (upgrade + downgrade)
- [ ] Всі тести проходять
