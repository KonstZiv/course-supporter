# S2-045: SlideVideoMapping Migration

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Status:** CLOSED (no-op — no production data to migrate)

---

## Context

Sprint 2 Epic 5 redesigned `slide_video_mappings` from a simple
`(course_id, slide_number, video_timecode)` tuple to a full-featured
mapping system with MaterialEntry FKs, three-level validation, and
JSONB error tracking.

The migration `a8f1e2c3d4b5_redesign_slide_video_mappings.py` was
created as part of S2-038 (ORM redesign) and applied to production
during the VPS deploy (2026-02-18).

## Decision: No Data Migration Needed

**Reason:** The production VPS was deployed for testing purposes only.
No real slide-video mappings existed in the old table at the time the
redesign migration was applied. The migration correctly uses
`DROP TABLE` + `CREATE TABLE` (not `ALTER TABLE`) because the schema
change is fundamental — the old and new structures share no FKs in
common:

| Old schema (S0)               | New schema (S2-038+)                  |
|-------------------------------|---------------------------------------|
| `course_id` → courses         | `node_id` → material_nodes            |
| `slide_number`                | `presentation_entry_id` → material_entries |
| `video_timecode` (single)     | `video_entry_id` → material_entries   |
| —                             | `video_timecode_start` + `_end`       |
| —                             | `validation_state`, JSONB errors      |

Even if data existed, a mechanical migration would be impossible
without heuristics to determine which MaterialEntry corresponds to
the "presentation" and "video" — information the old schema did not
store.

## Migration File

**`migrations/versions/a8f1e2c3d4b5_redesign_slide_video_mappings.py`**

- **upgrade:** DROP old table → CREATE new table with all FKs, indexes,
  and partial index on `validation_state != 'validated'`
- **downgrade:** DROP new table → recreate old schema (course_id-based)
- **Depends on:** `5129ac60d408` (material_nodes + material_entries)

## Existing Test Coverage

| Test | What it verifies |
|------|-----------------|
| `test_schema_sync::test_all_orm_tables_exist_in_db` | ORM tables match DB |
| `test_schema_sync::test_all_orm_columns_exist_in_db` | All ORM columns exist in DB |
| `test_schema_sync::test_alembic_head_matches_current` | DB revision = alembic head |

These integration tests (`requires_db` marker) validate that the
migration produces a schema matching the ORM definition. They run
against a live PostgreSQL instance (`docker compose up`).

## Schema Reference (Final State)

```
slide_video_mappings
├── id                      UUID PK (UUIDv7)
├── node_id                 UUID FK → material_nodes.id (CASCADE)
├── presentation_entry_id   UUID FK → material_entries.id (CASCADE)
├── video_entry_id          UUID FK → material_entries.id (CASCADE)
├── slide_number            INTEGER NOT NULL (≥ 1)
├── video_timecode_start    VARCHAR(20) NOT NULL (HH:MM:SS / MM:SS)
├── video_timecode_end      VARCHAR(20) nullable
├── order                   INTEGER NOT NULL DEFAULT 0
├── validation_state        ENUM(validated, pending_validation, validation_failed)
├── blocking_factors        JSONB nullable
├── validation_errors       JSONB nullable
├── validated_at            TIMESTAMPTZ nullable
└── created_at              TIMESTAMPTZ NOT NULL DEFAULT now()

Indexes:
  ix_svm_node           (node_id)
  ix_svm_presentation   (presentation_entry_id)
  ix_svm_video          (video_entry_id)
  ix_svm_validation     (validation_state) WHERE validation_state != 'validated'
```

## API Endpoints (as of S2-044)

| Method | Path | Description |
|--------|------|-------------|
| POST   | `/courses/{id}/nodes/{node_id}/slide-mapping` | Batch create with partial success (201/207/422) |
| GET    | `/courses/{id}/nodes/{node_id}/slide-mapping` | List mappings for node |
| DELETE | `/courses/{id}/slide-mapping/{mapping_id}`    | Delete single mapping |

## Validation Lifecycle

```
         ┌──────────┐
         │ Level 1  │  Always at creation:
         │Structural│  entries exist, correct source_type, timecode format
         └────┬─────┘
              │
    ┌─────────┴─────────┐
    │                   │
    ▼                   ▼
Both READY        Not all READY
    │                   │
    ▼                   ▼
┌──────────┐    ┌──────────────────┐
│ Level 2  │    │ PENDING_VALIDATION│
│ Content  │    │ + blocking_factors│
│slide/tc  │    └────────┬─────────┘
│ in range │             │
└────┬─────┘    ingestion completes
     │                   │
     ▼                   ▼
 VALIDATED      auto-revalidate (L1+L2)
   or                    │
VALIDATION_FAILED   VALIDATED / FAILED
```

## Checklist

- [x] Migration exists and works (`a8f1e2c3d4b5`)
- [x] No production data to migrate (confirmed)
- [x] Schema sync tests cover ORM ↔ DB alignment
- [x] `make check` passes (lint + mypy + 959 tests)
- [x] Documentation complete
