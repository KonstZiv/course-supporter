# Phase 4: Remove Course Entity (XL)

**Складність:** XL (Extra Large)
**Залежності:** Phases 0-3
**Задачі:** S3-011, S3-012, S3-013
**PRs:** 2-3 PRs (S3-011 окремо, S3-012+S3-013 разом)
**Risk:** HIGH

## Мета

Видалити Course як окрему сутність. Root MaterialNode (`parent_materialnode_id IS NULL`) = курс. URL API змінюються з `/courses/{id}/...` на `/nodes/{id}/...`.

## Чому це XL

Торкається **ВСІ** routes, **ВСІ** repositories, `enqueue.py`, `generation_orchestrator.py`, `tasks.py`, `schemas.py`, і **БІЛЬШІСТЬ** тестів (~1145).

## Sub-phases (стратегія мітігації)

1. **S3-011 (additive)** — додати поля до MaterialNode, data migration. Deploy безпечно — нічого не ламає.
2. **S3-012 (rewrite)** — переписати routes та repositories. Breaking change для API.
3. **S3-013 (drop)** — видалити Course table та все пов'язане.

## Breaking Changes

- `POST /courses` → `POST /nodes` (створює root node з tenant_id)
- `GET /courses` → `GET /nodes?root=true`
- `/courses/{id}/nodes/...` → `/nodes/{root_id}/children/...`
- `Job.course_id` → `Job.materialnode_id`
- `Snapshot.course_id` → `Snapshot.materialnode_id`

## Критерії завершення

- [ ] Course table видалена
- [ ] MaterialNode root = course
- [ ] Нові URL patterns працюють
- [ ] Всі ~1145 тестів проходять
- [ ] Production API працює з новими URL
