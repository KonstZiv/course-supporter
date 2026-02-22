# S2-046: Mapping validation unit tests — Деталі

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 4h → **Фактично:** ~1h

---

## Мета

Повне тестове покриття для валідації маппінгів — всі 3 рівні,
auto-revalidation, partial success, edge cases.

## Що зроблено

### Нові тести (16)

**Level 1 — timecode edge cases (9 тестів):**
- `test_invalid_timecode_end_format` — parametrized (7 values), перевірка
  невалідного `video_timecode_end` при валідному `tc_start`
- `test_timecode_end_equals_start_is_valid` — граничне значення `tc_end == tc_start`
- `test_both_timecodes_invalid` — обидва невалідні → два errors зібрані

**Level 2 — content extraction edge cases (6 тестів):**
- `test_multi_chunk_video_uses_max_end_sec` — реальний сценарій з 3 chunks,
  duration = max(end_sec)
- `test_page_count_zero_skips_slide_check` — `page_count=0` → skip L2
- `test_negative_page_count_skips_slide_check` — `page_count=-5` → skip L2
- `test_chunk_without_metadata_key_ignored` — chunk без `metadata` key
- `test_metadata_without_page_count_skips_slide_check` — metadata є, `page_count` ні
- `test_json_array_processed_content_skips_level2` — non-dict JSON (array)

**Auto-revalidation (1 тест):**
- `test_revalidate_batch_mixed_outcomes` — batch з двома маппінгами,
  один → VALIDATED, інший → VALIDATION_FAILED (slide out of range)

### Assertion hardening (3 існуючих тести)

Додано `validated_at is None` assertions до:
- `test_revalidate_material_becomes_error`
- `test_revalidate_one_ready_one_still_pending`
- `test_revalidate_ready_but_l2_fails` (+ `blocking_factors is None`)

## Покриття за рівнями

| Рівень | Було | Стало | Категорії |
|--------|------|-------|-----------|
| L1 (structural) | 17 | 26 | entry checks, UUID, timecode format/ordering |
| L2 (content) | 13 | 19 | slide range, timecode range, metadata edge cases |
| L3 (deferred) | 10 | 10 | blocking factors, mixed states |
| Revalidation | 7 | 8 | lifecycle, mixed batch outcomes |
| Route integration | 4 | 4 | 422/207 status codes |
| **Всього** | **58** | **74** | |

## Результати

```
tests/unit/test_mapping_validation.py — 74 passed (1.67s)
tests/unit/test_api/test_slide_mapping.py — 30 passed
make check — 975 passed, 27 skipped
```

## Checklist

- [x] Код проходить `make check` (ruff + mypy + pytest)
- [x] Edge cases покриті (timecode_end, multi-chunk, page_count=0, non-dict JSON)
- [x] Assertion hardening на revalidation тестах
- [x] Documentation оновлена
