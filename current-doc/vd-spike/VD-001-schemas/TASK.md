# VD-001: Pydantic моделі (schemas.py)

**Фаза:** 4 — Implementation
**Пріоритет:** Критичний (блокує всі implementation таски)
**Залежності:** Spike A + B завершені

## Що робимо

Створюємо Pydantic моделі для VD pipeline на основі spike JSON структур.

## Яким чином

Створити `src/course_supporter/vd/schemas.py` з моделями:

### Stage A: Frame Sampling

- `SampledFrame` — frame_id, filename, timestamp_sec, scene_id, dhash, hamming_from_prev, is_gap_fill
- `Scene` — scene_id, frame_ids, start_sec, end_sec
- `FrameSamplingResult` — frames, scenes, pip_mask, video_resolution, sampling_params

### Stage B: Visual Analysis

- `EyesResult` — frame_id, scene_id, response (raw Markdown), context_frames, model
- `InstantMemory` — scene_id, merged_text, frame_count
- `SceneMemory` — scene_id, scene_type, summary, complete_text, topics, importance
- `CourseMemory` — text (≤200 words), scenes_covered
- `SceneAnalysis` — scene, eyes_results, instant_memory, scene_memory
- `VDResult` — scenes, course_memory, frames_total, frames_analyzed, model

### Cross-modal Alignment (ingestion level)

- `AlignedSegment` — start_sec, end_sec, stt_text, vd_scene, semantic_overlap, conflicts, alignment_confidence
- `AlignmentReport` — coverage_gaps, vd_orphans, stt_orphans, conflicts, semantic_coverage

## Acceptance criteria

- [ ] Всі models мають валідацію і serialization
- [ ] `mypy --strict` проходить
- [ ] Відповідають реальним spike JSON структурам з `VD-SPIKE-B/pipeline/`
- [ ] `__init__.py` створений для `src/course_supporter/vd/`
