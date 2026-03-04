# Phase 7: FK Rename + DB Comments

**Складність:** L (Large)
**Залежності:** Phase 6 (all tables finalized)
**Задачі:** S3-016
**PR:** 1 PR

## Мета

Консистентні імена FK: `{tablename}_id` + PostgreSQL `COMMENT ON` для всіх таблиць і колонок.

## FK Renames

| Старе | Нове | Таблиця |
|-------|------|---------|
| `parent_id` | `parent_materialnode_id` | material_nodes |
| `node_id` | `materialnode_id` | material_entries, slide_video_mappings |
| `pending_job_id` | `job_id` | material_entries |
| `presentation_entry_id` | `presentation_materialentry_id` | slide_video_mappings |
| `video_entry_id` | `video_materialentry_id` | slide_video_mappings |

**Note:** StructureNode FK names already follow convention (created in Phase 6).

## DB Comments

`COMMENT ON TABLE` та `COMMENT ON COLUMN` для всіх 9 таблиць. Видимі в psql (`\dt+`, `\d+ table`), pgAdmin, DBeaver.

## Критерії завершення

- [ ] Всі FK відповідають `{tablename}_id` конвенції
- [ ] `comment=` на кожному `mapped_column()` в orm.py
- [ ] `COMMENT ON TABLE` для всіх 9 таблиць
- [ ] `\dt+` в psql показує коментарі
