# S2-044: Mapping CRUD — GET list + DELETE

## Summary

Added two CRUD endpoints for slide-video mappings:

- `GET /api/v1/courses/{id}/nodes/{node_id}/slide-mapping` — list mappings for a node
- `DELETE /api/v1/courses/{id}/slide-mapping/{mapping_id}` — delete a single mapping

## Changes

### Repository (`repositories.py`)
- `SlideVideoMappingRepository.get_by_id()` — fetch single mapping by PK
- `SlideVideoMappingRepository.delete()` — accepts ORM object directly (no redundant SELECT)

### Schema (`schemas.py`)
- Enriched `SlideVideoMapItemResponse` with `Field(description=...)` for all fields
- `ValidationState` StrEnum for `validation_state` field (OpenAPI enum)
- `BlockingFactorResponse` / `ValidationErrorResponse` typed models (replace `dict[str, object]`)
- Added `SlideVideoMapListResponse` wrapper with `items` + `total` (pagination-ready)

### Routes (`routes/courses.py`)
- `list_slide_mappings` — GET, SharedDep (read-only), returns ordered mappings
- `delete_slide_mapping` — DELETE 204, PrepDep (mutation), ownership chain: mapping → node → course

### Tests (`test_slide_mapping.py`)
- 12 new tests (4 GET list, 4 DELETE, 4 repository)
- Total: 31 tests in file

## Design Decisions

- **DELETE at course level** (no node_id in path) — mapping_id is globally unique
- **Ownership verification**: mapping → node_id → node.course_id == course_id
- **Empty list = 200** (not 404) — node with no mappings is a valid state
- **SlideVideoMapListResponse wrapper** — enables future pagination (limit/offset + count query planned)
