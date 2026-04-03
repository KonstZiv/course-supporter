# VD-001: Pydantic моделі (schemas.py)

**Фаза:** 4 — Implementation
**Пріоритет:** Критичний (блокує всі implementation таски)
**Залежності:** Spike A + B завершені
**Статус:** ✅ DONE + оновлено після CP-2/CP-3

## Що зроблено

`src/course_supporter/vd/schemas.py` — Pydantic моделі для VD pipeline.

## Stage A: Frame Sampling

- `FrameSource` — enum: `golden`, `gap_fill`
- `ChangeClass` — enum: `first`, `boundary`, `medium`, `low`
- `SampledFrame` — frame_id, filename, timestamp_sec, scene_id, source, dhash, dhash_dist (0.0–1.0), time_gap, change_class
- `Scene` — scene_id, frame_ids, start_sec, end_sec, property `duration_sec`
- `PiPMask` — x, y, width, height, confidence (Picture-in-Picture overlay)
- `SamplingParams` — fps, hash_size, gap_fill_max_sec, scene boundaries (3-gate: dhash + color_hist + flow_coherence), tier1/tier2 dedup thresholds, min_votes
- `FrameSamplingResult` — frames, scenes, pip_mask, video_resolution, sampling_params

## Stage B: Visual Analysis + Streaming Memory

- `DeltaStrategy` — enum: `none`, `explicit`, `conditional`
- `EyesResult` — frame_id, scene_id, response (raw Markdown), n_images, latency_sec, input/output_tokens, description (≤300 chars), scene_type, is_delta, base_frame_id, importance
- `InstantMemory` — rolling 2-frame window (no LLM): frame_id, current (Eyes description), previous (compressed), is_delta
- `SceneMemory` — rolling per-scene LLM assessment: summary (English), scene_type, topics, importance (1-5), frames_seen, previous_scene_summary
- `VideoMemory` — running video context (English, ≤200 words): text, scenes_processed
- `SceneAnalysis` — scene + eyes_results + scene_memory
- `VDResult` — scenes, video_memory, frames_total, frames_analyzed, model

## Cross-modal Alignment (ingestion level, NOT part of VD)

- `AlignedSegment` — start_sec, end_sec, stt_text, vd_scene_id, vd_summary, semantic_overlap, conflicts, alignment_confidence
- `CoverageGap` — start_sec, end_sec, gap_type
- `AlignmentReport` — segments, coverage_gaps, vd_orphans, stt_orphans, conflicts, semantic_coverage

## Acceptance criteria

- [x] Всі models мають валідацію і serialization
- [x] `mypy --strict` проходить
- [x] `__init__.py` створений для `src/course_supporter/vd/`
- [x] 20 schema tests green
- [x] Streaming memory model (InstantMemory/SceneMemory/VideoMemory) замінив batch model (MergeMethod/CourseMemory)
