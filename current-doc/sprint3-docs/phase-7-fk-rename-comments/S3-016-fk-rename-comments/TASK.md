# S3-016: FK Rename + PostgreSQL COMMENT ON

**Phase:** 7 (FK Rename + DB Comments)
**Складність:** L
**Статус:** PENDING
**Залежність:** S3-015 (StructureNode — all tables finalized)

## FK Renames

### material_nodes
- `parent_id` → `parent_materialnode_id`

### material_entries
- `node_id` → `materialnode_id`
- `pending_job_id` → `job_id`

### slide_video_mappings
- `node_id` → `materialnode_id`
- `presentation_entry_id` → `presentation_materialentry_id`
- `video_entry_id` → `video_materialentry_id`

### structure_snapshots
(Може бути вже перейменований в Phase 3/4)
- `node_id` → `materialnode_id` (if not done)

### structure_nodes
(Вже з правильними іменами з Phase 6)

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/orm.py` | Rename FK columns + add `comment=` on mapped_columns + `__table_args__ comment` |
| `src/course_supporter/storage/*.py` | Update all repository queries that use old FK names |
| `src/course_supporter/api/schemas.py` | Update field names in schemas |
| `src/course_supporter/api/routes/*.py` | Update field references |
| `src/course_supporter/api/tasks.py` | Update field references |
| `src/course_supporter/generation_orchestrator.py` | Update field references |
| `src/course_supporter/enqueue.py` | Update field references |
| `migrations/versions/` | `op.alter_column()` for renames + `COMMENT ON` |
| `tests/` | Update all references to old FK names |

## Migration

```python
def upgrade():
    # FK renames
    op.alter_column("material_nodes", "parent_id",
                    new_column_name="parent_materialnode_id")
    op.alter_column("material_entries", "node_id",
                    new_column_name="materialnode_id")
    op.alter_column("material_entries", "pending_job_id",
                    new_column_name="job_id")
    op.alter_column("slide_video_mappings", "node_id",
                    new_column_name="materialnode_id")
    op.alter_column("slide_video_mappings", "presentation_entry_id",
                    new_column_name="presentation_materialentry_id")
    op.alter_column("slide_video_mappings", "video_entry_id",
                    new_column_name="video_materialentry_id")

    # COMMENT ON TABLE for all 9 tables
    op.execute("COMMENT ON TABLE tenants IS 'Multi-tenant organizations'")
    op.execute("COMMENT ON TABLE api_keys IS 'Authentication keys with scope-based access control'")
    op.execute("COMMENT ON TABLE material_nodes IS 'Hierarchical tree of course materials. Root node (parent IS NULL) = course'")
    op.execute("COMMENT ON TABLE material_entries IS 'Individual learning materials (video, presentation, text, web)'")
    op.execute("COMMENT ON TABLE slide_video_mappings IS 'Presentation slide to video timecode mappings'")
    op.execute("COMMENT ON TABLE structure_snapshots IS 'LLM-generated course structure versions'")
    op.execute("COMMENT ON TABLE structure_nodes IS 'Recursive tree of generated course structure elements'")
    op.execute("COMMENT ON TABLE jobs IS 'Background task queue entries (ingestion, generation)'")
    op.execute("COMMENT ON TABLE external_service_calls IS 'Audit log of all external API calls (LLM, transcription, etc.)'")

    # COMMENT ON COLUMN for non-obvious fields (selected examples)
    op.execute("COMMENT ON COLUMN api_keys.key_hash IS 'SHA-256 hash of the API key. Raw key is never stored'")
    op.execute("COMMENT ON COLUMN material_nodes.node_fingerprint IS 'Merkle hash of content subtree for change detection'")
    op.execute("COMMENT ON COLUMN material_entries.processed_hash IS 'SHA-256 of processed_content for Merkle tree'")
    # ... more column comments
```

## ORM comment= parameter

```python
class MaterialNode(Base):
    __tablename__ = "material_nodes"
    __table_args__ = (
        {"comment": "Hierarchical tree of course materials. Root node (parent IS NULL) = course"},
    )

    parent_materialnode_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("material_nodes.id"),
        comment="Self-referential FK. NULL = root node (course level)"
    )
    node_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        comment="Merkle hash of content subtree. NULL = stale, needs recompute"
    )
```

## Підхід

1. IDE search-replace для всіх старих FK names
2. Масивний але механічний — ризик MEDIUM
3. CI як gate — PR не мержиться поки всі тести не green

## Acceptance Criteria

- [ ] Всі FK відповідають `{tablename}_id` конвенції
- [ ] `comment=` на mapped_column() для non-obvious полів
- [ ] `COMMENT ON TABLE` для всіх 9 таблиць
- [ ] `COMMENT ON COLUMN` для FK, JSONB, nullable, derived полів
- [ ] Migration проходить
- [ ] Всі тести проходять
- [ ] `\dt+` в psql показує table comments
