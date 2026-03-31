# VD-011: Factory Wiring

**Фаза:** 5 — Integration
**Пріоритет:** High
**Залежності:** VD-007, VD-009

## Що робимо

Оновлюємо factory для створення `VideoProcessor` з усіма залежностями VD pipeline. Wiring нових компонентів через існуючий factory pattern.

## Яким чином

Оновити `src/course_supporter/ingestion/factory.py`:

1. **Додати `FrameSampleFunc` до `HeavySteps`:**
   - `HeavySteps` — контейнер для "важких" залежностей (FFmpeg, Vision LLM, тощо)
   - Додати frame sampling function як одну з heavy steps

2. **Factory function `create_vd_pipeline()`:**
   - Створює `VDPipeline` з усіма залежностями:
     - `FrameSampler`
     - `PiPTracker`
     - `VisualAnalyzer`
     - `OCRExtractor` (optional, якщо Stage C потрібен)
     - `AggregationLayer`
   - Передає `ModelRouter` та `asyncio.Semaphore` для concurrency control

3. **Wire into `VideoProcessor` creation:**
   - Оновити існуючий factory для `VideoProcessor`
   - Передати `STTRouter`, `VDPipeline`, `AggregationLayer`
   - Видалити старий wiring для `GeminiVideoProcessor`/`WhisperVideoProcessor`

4. **Conditional OCR:**
   - Якщо конфігурація вказує що Stage C потрібен → створити `OCRExtractor`
   - Інакше → `None` (VDPipeline пропустить Stage C)

## Результат

- Оновлений `src/course_supporter/ingestion/factory.py`
- `create_vd_pipeline()` factory function
- `VideoProcessor` створюється з правильними залежностями
- Старий wiring видалений

## Як перевіряємо

```bash
uv run mypy src/course_supporter/ingestion/factory.py  # strict, no errors
uv run ruff check src/course_supporter/ingestion/       # no lint errors
uv run python -c "
from course_supporter.ingestion.factory import create_video_processor
# Перевірити що factory створює VideoProcessor з VDPipeline
processor = create_video_processor(...)
print(type(processor))
"
```
