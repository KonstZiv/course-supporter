# VD-009: Redesign VideoProcessor

**Фаза:** 5 — Integration
**Пріоритет:** Critical
**Залежності:** VD-007

## Що робимо

Повністю переписуємо `VideoProcessor` в `src/course_supporter/ingestion/video.py`. Замість старого підходу (відправити все відео до Gemini Vision) — паралельний STT + VD pipeline з aggregation.

## Яким чином

Модифікувати `src/course_supporter/ingestion/video.py`:

1. **Видалити старі класи:**
   - `GeminiVideoProcessor` — був primary (Gemini Vision для всього відео)
   - `WhisperVideoProcessor` — був fallback (local Whisper)

2. **Новий `VideoProcessor`:**
   - Приймає `STTRouter`, `VDPipeline`, `AggregationLayer` через DI
   - Реалізує `SourceProcessor` interface

3. **Parallel execution:**
   ```python
   async def process(video_path) -> SourceDocument:
       # Паралельно:
       stt_task = asyncio.create_task(self.stt_router.transcribe(audio_path))
       frames = await self.vd_pipeline.extract_frames(video_path)

       stt_result = await stt_task
       # Visual analysis з STT context (approach B)
       vd_result = await self.vd_pipeline.analyze(frames, stt_context=stt_result)

       # Aggregation
       chunks = await self.aggregation.merge(stt_result, vd_result)
       return SourceDocument(chunks=chunks, metadata=...)
   ```

4. **SourceDocument metadata:**
   - `strategy: "stt+vd"` — нова стратегія
   - `stt_provider: str` — який STT provider використано
   - `vd_frames_total: int` — скільки кадрів проаналізовано
   - `pip_events: int` — кількість PiP events

5. **Audio extraction:**
   - FFmpeg для витягування audio track як MP3
   - Передати до `STTRouter` для транскрипції

## Результат

- Оновлений `src/course_supporter/ingestion/video.py`
- Новий `VideoProcessor` замість `GeminiVideoProcessor`/`WhisperVideoProcessor`
- Паралельний STT + VD з aggregation
- `SourceDocument` з розширеним metadata

## Як перевіряємо

```bash
uv run mypy src/course_supporter/ingestion/video.py    # strict, no errors
uv run ruff check src/course_supporter/ingestion/       # no lint errors
uv run pytest tests/unit/test_video_processor.py -v     # existing tests pass (adapted)
# E2E: після VD-014
```
