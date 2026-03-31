# VD-001: Pydantic моделі (schemas.py)

**Фаза:** 4 — Implementation
**Пріоритет:** Критичний (блокує всі implementation таски)
**Залежності:** Spike A + B завершені

## Що робимо

Створюємо Pydantic моделі для всіх stages VD pipeline: frame sampling, PiP tracking, visual analysis, OCR (умовний), aggregation.

## Яким чином

Створити `src/course_supporter/vd/schemas.py` з моделями:

```python
# Stage A: Frame Sampling
class Rect(BaseModel): ...               # bounding box (x1, y1, x2, y2)
class PiPMask(BaseModel): ...            # zone_name, rect, active
class PiPEvent(BaseModel): ...           # timestamp, zone, method, confidence
class SampledFrame(BaseModel): ...       # index, timestamp, path, dhash, hamming, pip_mask
class FrameSamplingResult(BaseModel): ... # frames, pip_events, total_raw, params, resolution

# Stage B: Visual Analysis
class FrameClassification(BaseModel): ... # Pass 1: scene_type, description, has_text, importance
class VisualAnalysis(BaseModel): ...      # Pass 2: frame_range, timestamps, description, extracted_text
class VisualAnalysisResult(BaseModel): ... # analyses, frames_analyzed, cost

# Stage C: OCR (умовний)
class OCRExtraction(BaseModel): ...       # frame_index, raw_text, corrected_text, type, language

# Stage D: Aggregation
# Використовує існуючі ContentChunk з models/source.py
```

Дотримуватись паттернів з `stt/schemas.py` (STTRequest, STTResult, STTSegment).

## Результат

- Файл `src/course_supporter/vd/schemas.py`
- Файл `src/course_supporter/vd/__init__.py` з re-exports
- Всі моделі з type hints (mypy strict)
- Docstrings англійською

## Як перевіряємо

```bash
uv run mypy src/course_supporter/vd/schemas.py   # strict, no errors
uv run ruff check src/course_supporter/vd/        # no lint errors
uv run python -c "from course_supporter.vd.schemas import SampledFrame, VisualAnalysis"
```
