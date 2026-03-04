# Sprint 3: TO-BE Schema Migration + Full Pipeline

## Goal

Мігрувати з 14-table AS-IS schema на 9-table TO-BE архітектуру, виправити production баги, завершити документацію, побудувати recursive generation pipeline та систему file upload.

**Цільова архітектура:** 9 таблиць (Tenant, APIKey, MaterialNode, MaterialEntry, SlideVideoMapping, StructureSnapshot, StructureNode, Job, ExternalServiceCall).

**Breaking change:** URL API змінюються з `/courses/{id}/...` на `/nodes/{id}/...`.

**Повна специфікація TO-BE:** `current-doc/erd-course-vs-rootnode.md`
**Деталі рішень:** `current-doc/backlog.md`

---

## Phases Overview

| Phase | Назва | Складність | Залежності | Задачі |
|-------|-------|-----------|------------|--------|
| 0 | Production Blockers | S | — | S3-001, S3-002 |
| 1 | Незалежні cleanup | 5×S | — | S3-003..S3-007 |
| 2 | ExternalServiceCall + Config | M | — | S3-008, S3-009 |
| 3 | Snapshot Simplification | M | Phase 2 | S3-010 |
| 4 | Remove Course Entity | XL | Phase 0–3 | S3-011..S3-013 |
| 5 | Remove SourceMaterial | M | Phase 4 | S3-014 |
| 6 | StructureNode | XL | Phase 5 | S3-015 |
| 7 | FK Rename + DB Comments | L | Phase 6 | S3-016 |
| 8 | Documentation | M | Phase 7 | S3-017, S3-018 |
| 9 | Cascading Job Failure | M | Phase 1 | S3-019 |
| 10 | Recursive Generation | XL | Phase 6, 9 | S3-020 |
| 11 | Full File Upload | L | Phase 4 | S3-021 |

---

## Dependency Graph

```
Phase 0 (bugs) ──→ Phase 1 (cleanup, parallel) ──→ Phase 2 (ESC + config)
                                                        │
                                                        ▼
                                                   Phase 3 (Snapshot)
                                                        │
                                                        ▼
                                                   Phase 4 (Remove Course, XL)
                                                        │
                                                        ▼
                                                   Phase 5 (Remove SourceMaterial)
                                                        │
                                                        ▼
                                                   Phase 6 (StructureNode, XL)
                                                        │
                                                        ▼
                                                   Phase 7 (FK rename + COMMENT ON)
                                                        │
                                                        ▼
                                                   Phase 8 (Documentation)
                                                        │
                                                        ▼
                                                   Phase 9 (Cascading failure)
                                                        │
                                                        ▼
                                                   Phase 10 (Recursive generation, XL)

Phase 4 ──→ Phase 11 (File upload, parallel with Phase 8+)
```

## Parallelism

- Phase 0 + Phase 1 — одночасно
- Phase 1 tasks (S3-003..S3-007) — між собою паралельно
- Phase 11 — після Phase 4, паралельно з Phases 8–10

## Risk Summary

| Phase | Ризик | Мітігація |
|-------|-------|-----------|
| 4 (Course) | HIGH — all routes, repos, tests | Sub-phases: additive → rewrite → drop |
| 6 (StructureNode) | HIGH — нова recursive table + data migration | LLM output format незмінний |
| 7 (FK rename) | MEDIUM — механічний але масивний | IDE search-replace, CI gate |
| 10 (Recursive gen) | MEDIUM — multi-pass LLM | Incremental (pass by pass) |

## Total Tasks: 21 (S3-001 .. S3-021)
