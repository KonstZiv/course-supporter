# S3-018: Endpoint Reference

**Phase:** 8 (Documentation)
**Складність:** M
**Статус:** PENDING
**Залежність:** S3-016 (final schema + URL patterns)

## Контекст

Продовження S2-059 (Endpoint Reference). Повна довідка всіх endpoints з новими field names та URL patterns.

## Deliverable

9 файлів в `docs/api/reference/`:

1. `nodes.md` — Node CRUD (root = course), tree, children
2. `materials.md` — MaterialEntry CRUD, file upload
3. `generation.md` — Structure generation, snapshots
4. `structure.md` — StructureNode tree browsing
5. `mappings.md` — SlideVideoMapping CRUD, validation
6. `jobs.md` — Job status, queue estimate
7. `reports.md` — ExternalServiceCall reports, costs
8. `admin.md` — Tenant, APIKey management
9. `health.md` — Health check

## Для кожного endpoint

- HTTP method + URL pattern
- Request parameters (path, query, body)
- Request body schema (JSON example)
- Response schema (JSON example)
- Status codes (200/201/207/400/404/422/500)
- curl приклад
- Нотатки (rate limits, scopes, тощо)

## Acceptance Criteria

- [ ] Всі endpoints задокументовані з новими URL
- [ ] JSON examples для request/response
- [ ] curl приклади
- [ ] Deployed на GitHub Pages
