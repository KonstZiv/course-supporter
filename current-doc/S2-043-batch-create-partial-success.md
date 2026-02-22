# S2-043: Batch Create Endpoint — Partial Success + Deduplication

## Status: DONE

## Summary

Changed `POST /courses/{id}/nodes/{node_id}/slide-mapping` from all-or-nothing
to **partial success** semantics. Valid mappings are now created even when some
fail validation. Added **natural key deduplication** for safe retry after
partial failure.

## Acceptance Criteria

- [x] Valid mappings are created even when some fail validation
- [x] Duplicate mappings (same natural key) are silently skipped
- [x] HTTP 201 when all succeed (or only duplicates skipped)
- [x] HTTP 207 when partial success (some created, some rejected)
- [x] HTTP 422 only when ALL mappings fail
- [x] Response includes `created`, `skipped`, `failed` counts
- [x] Response includes `rejected[]` with per-item errors and original index
- [x] Response includes `skipped_items[]` with duplicate hint
- [x] Response includes `hints` (resubmit guidance) only when rejected > 0
- [x] All existing tests updated, new tests added

## Natural Key

A mapping is considered a duplicate if it matches an existing record on:

```
(presentation_entry_id, video_entry_id, slide_number, video_timecode_start)
```

## HTTP Status Codes

| Scenario | Status |
|---|---|
| All created (no rejected) | 201 |
| Only duplicates skipped, none rejected | 201 |
| Some created + some rejected | 207 |
| All rejected (none created) | 422 |

## Response Schema

```json
{
  "created": 2,
  "skipped": 1,
  "failed": 1,
  "mappings": [/* created records */],
  "skipped_items": [{"index": 0, "hint": "already exists"}],
  "rejected": [{"index": 3, "errors": [{"field": "...", "message": "...", "hint": "..."}]}],
  "hints": {
    "resubmit": "Fix errors in rejected items and resubmit only those. Already created mappings will be automatically skipped.",
    "batch_size": "If the batch keeps failing, try reducing batch size."
  }
}
```

## Files Modified

- `src/course_supporter/api/schemas.py` — added `RejectedMappingResponse`, `SkippedMappingResponse`; expanded `SlideVideoMapResponse`
- `src/course_supporter/api/routes/courses.py` — partial success + dedup logic
- `tests/unit/test_api/test_slide_mapping.py` — 8 new route tests
- `tests/unit/test_mapping_validation.py` — updated `TestRouteReturns422OnValidationError` (422 only when ALL fail, added 207 test)

## Test Count

- `test_slide_mapping.py`: 16 tests (was 10)
- `test_mapping_validation.py`: 61 tests (was 59, +2 route tests)
- Total project: 948 passed
