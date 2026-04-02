# VD-002: Frame Sampler (frame_sampler.py)

**Фаза:** 4 — Implementation
**Пріоритет:** Високий
**Залежності:** VD-001

## Що робимо

Port `scripts/spike_frame_sampling.py` в production module з async interface. Включає PiP detection (раніше VD-002 pip_tracker — тепер частина цього модуля).

## Яким чином

Створити `src/course_supporter/vd/frame_sampler.py`:

### FrameSampler class

Параметри (spike-proven):
- fps=0.5, hash_size=16, dhash_threshold=5%
- gap_fill_max_sec=15.0
- scene_boundary: dHash >20% OR time_gap >10s
- cooldown: 4s, 3 consecutive
- PiP detection via temporal diff (8 zones, confidence threshold)

### Pipeline кроки:
1. FFmpeg fps extraction → temp JPEG files (async subprocess)
2. PiP detection via temporal diff → mask rect
3. dHash computation (з PiP mask) → dedup
4. Gap fill (no gap > 15s)
5. Scene boundary detection → Scene grouping
6. Return FrameSamplingResult

### Залежності
- opencv-python-headless, Pillow, imagehash
- FFmpeg (system binary)

## Acceptance criteria

- [ ] 10-хв відео → ~17 frames (як spike)
- [ ] 27-хв відео → ~91 frames
- [ ] PiP detection confidence >0.5
- [ ] Async FFmpeg через asyncio.create_subprocess_exec
- [ ] PiP mask correct на 5+ перевірених кадрах

## ⚠️ Після завершення — виконати CP-1: Перевірка Frame Sampler

Див. VD-IMPLEMENTATION-PLAN.md, секція CP-1. Людина перевіряє: покриття, дублікати, PiP mask, scene boundaries, gap fill. ~30 хвилин.
