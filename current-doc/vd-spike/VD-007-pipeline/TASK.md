# VD-007: Pipeline Orchestrator

**Фаза:** 4 — Implementation
**Пріоритет:** Critical
**Залежності:** VD-002, VD-003, VD-004, VD-005 (optional), VD-006

## Що робимо

Створюємо `VDPipeline` клас — головний orchestrator, який послідовно запускає всі stages VD pipeline: frame extraction → visual analysis → (optional OCR) → aggregation.

## Яким чином

Створити `src/course_supporter/vd/pipeline.py`:

1. **VDPipeline клас:**
   - Ін'єкція залежностей: `FrameSampler`, `PiPTracker`, `VisualAnalyzer`, optional `OCRExtractor`, `AggregationLayer`
   - Головний метод `process(video_path, stt_result?) -> list[ContentChunk]`

2. **Orchestration flow:**
   ```
   Stage A: extract_frames(video_path)
     → PiP tracking + scene detection + dHash dedup
     → FrameSamplingResult

   Stage B: analyze(frames, stt_context?)
     → Pass 1: classify + filter
     → Pass 2: detailed analysis
     → VisualAnalysisResult

   Stage C (optional): ocr(frames)
     → OCR + LLM correction
     → list[OCRExtraction]

   Stage D: aggregate(stt_result, vd_result, ocr_result?)
     → Merge + cross-reference + dedup
     → list[ContentChunk]
   ```

3. **Temp directory management:**
   - Створити `tempfile.mkdtemp()` для зберігання extracted frames
   - Обгорнути весь pipeline в `try/finally` з `shutil.rmtree()` для cleanup
   - Гарантувати cleanup навіть при exceptions

4. **Concurrency control:**
   - `VISION_LLM_CONCURRENCY = 5` — максимум паралельних Vision LLM запитів
   - Передати `asyncio.Semaphore` в `VisualAnalyzer`

5. **Logging:**
   - `structlog` для кожного stage: старт, кількість frames, тривалість, помилки
   - Логувати загальний час обробки pipeline

## Результат

- Файл `src/course_supporter/vd/pipeline.py`
- `VDPipeline` клас з `process()` методом
- Orchestration: Stage A → B → (C) → D
- Temp directory management з гарантованим cleanup
- Concurrency control через `asyncio.Semaphore`

## Як перевіряємо

```bash
uv run mypy src/course_supporter/vd/pipeline.py    # strict, no errors
uv run ruff check src/course_supporter/vd/          # no lint errors
# Integration: запустити на sample.mp4 — отримати list[ContentChunk]
```
