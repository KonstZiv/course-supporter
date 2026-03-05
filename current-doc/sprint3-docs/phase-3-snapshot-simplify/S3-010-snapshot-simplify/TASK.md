# S3-010: Snapshot Simplification

**Phase:** 3 (Snapshot Simplification)
**Складність:** M
**Статус:** PENDING
**Залежність:** S3-008 (ExternalServiceCall must exist)

## Контекст

`CourseStructureSnapshot` дублює метадані LLM виклику (prompt_version, model_id, tokens_in/out, cost_usd) які вже зберігаються в LLMCall (→ ExternalServiceCall). Єдине джерело правди — ExternalServiceCall через FK.

Рішення (зафіксоване в `current-doc/backlog.md`): 6 полів total.

## Target Schema

```
StructureSnapshot:
  id              UUID PK
  materialnode_id UUID FK → MaterialNode (target subtree)
  externalservicecall_id UUID FK → ExternalServiceCall (call details)
  node_fingerprint String(64)  (Merkle hash for idempotency)
  structure       JSONB        (raw LLM response)
  created_at      DateTime(tz)
```

**Видаляються:** `mode`, `prompt_version`, `model_id`, `tokens_in`, `tokens_out`, `cost_usd`, `course_id` (replaced by materialnode_id).

**Примітка:** `mode` (free/guided) визначається через strategy/prompt в ExternalServiceCall + external_services.yaml.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/orm.py` | Rewrite CourseStructureSnapshot → StructureSnapshot |
| `src/course_supporter/storage/snapshot_repository.py` | Rename, update queries |
| `src/course_supporter/api/tasks.py` | `arq_generate_structure` — create ESC + Snapshot pair |
| `src/course_supporter/api/routes/generation.py` | Update response (join ESC for metadata) |
| `src/course_supporter/api/schemas.py` | StructureSnapshotResponse з nested ESC info |
| `src/course_supporter/generation_orchestrator.py` | Update fingerprint check |
| `migrations/versions/` | Rename table + DROP columns + ADD FK |

## Migration

```python
def upgrade():
    # 1. Rename table
    op.rename_table("course_structure_snapshots", "structure_snapshots")

    # 2. Add FK to ExternalServiceCall
    op.add_column("structure_snapshots",
        sa.Column("externalservicecall_id", sa.Uuid(),
                  sa.ForeignKey("external_service_calls.id"), nullable=True))

    # 3. Data migration: create ExternalServiceCall for each existing snapshot
    # (copy model_id, tokens_in→unit_in, tokens_out→unit_out, cost_usd, prompt_version→prompt_ref)

    # 4. Make FK NOT NULL after data migration
    op.alter_column("structure_snapshots", "externalservicecall_id", nullable=False)

    # 5. Drop duplicated columns
    op.drop_column("structure_snapshots", "prompt_version")
    op.drop_column("structure_snapshots", "model_id")
    op.drop_column("structure_snapshots", "tokens_in")
    op.drop_column("structure_snapshots", "tokens_out")
    op.drop_column("structure_snapshots", "cost_usd")
    op.drop_column("structure_snapshots", "mode")

    # 6. Rename node_id → materialnode_id (Phase 7 convention, can do early)
    # OR leave for Phase 7 to minimize risk

    # 7. Drop course_id FK (will be handled in Phase 4 or here)
```

**Увага:** Data migration потрібна — існуючі snapshots на production мають metadata.

## Acceptance Criteria

- [ ] Table renamed to `structure_snapshots`
- [ ] 6 fields only (per spec)
- [ ] FK to ExternalServiceCall
- [ ] Data migration preserves existing metadata
- [ ] Generation pipeline creates ESC + Snapshot pair
- [ ] API returns LLM metadata via joined ESC
- [ ] Idempotency check (node_fingerprint) still works
