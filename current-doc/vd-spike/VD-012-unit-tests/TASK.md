# VD-012: Unit Tests

**Фаза:** 6 — Polish
**Пріоритет:** Середній
**Залежності:** VD-005, CP-4

## Що робимо

Unit tests для кожного VD module.

## Яким чином

- `tests/unit/test_frame_sampler.py` — mock FFmpeg, test dHash/PiP/gap_fill/scenes
- `tests/unit/test_visual_analyzer.py` — mock Gemini, test prompt building, rate limiting
- `tests/unit/test_memory_pipeline.py` — test merge logic, overlap detection, scene synthesis
- `tests/unit/test_alignment.py` — test temporal matching, conflict detection, verification

## Acceptance criteria

- [ ] Coverage > 80% для vd/ module
- [ ] Всі тести використовують pytest fixtures (не unittest classes)
- [ ] LLM calls замокані (без реальних API calls)
