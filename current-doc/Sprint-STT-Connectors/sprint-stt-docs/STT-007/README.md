# STT-007: Factory + VideoProcessor інтеграція

**Sprint:** STT Connectors
**Оцінка:** 4h

---

## Мета

Підключити Orchestrator до VideoProcessor. Повний редизайн: Gemini+Whisper composite → thin STT wrapper.

## Що робимо

1. Factory functions для створення providers і orchestrator (DI)
2. **Повний редизайн VideoProcessor**: замість composite Gemini→Whisper → тонка обгортка навколо STT chain
3. GeminiVideoProcessor / WhisperVideoProcessor залишаються як legacy (не видаляємо)
4. Оновити factory.py (ingestion) і worker DI wiring

## Як робимо (коротко)

Створити `src/course_supporter/stt/factory.py`:
  - `create_stt_providers(settings: STTSettings) → list[STTProvider]`
  - `create_transcription_orchestrator(settings, repo) → TranscriptionOrchestrator`

Модифікувати `VideoProcessor` (ingestion/video.py):
  - **БУЛО:** composite з `GeminiVideoProcessor` primary + `WhisperVideoProcessor` fallback
  - **СТАЛО:** thin wrapper: `__init__(orchestrator)`, `process()` = extract audio → STT → SourceDocument
  - `_extract_audio()` залишається (yt-dlp + ffmpeg — existing pipeline)
  - `_to_source_document()`: segments → ContentChunk(TRANSCRIPT), metadata з STT info
  - GeminiVideoProcessor / WhisperVideoProcessor — **не видаляємо**, залишаються як окремі класи

Оновити `ingestion/factory.py`:
  - `create_processors()` приймає `orchestrator` parameter
  - VideoProcessor створюється з orchestrator замість TranscribeFunc

Оновити worker DI (api/tasks.py або worker.py):
  - Створити orchestrator з settings + service_call_repo
  - Передати в create_processors

## Очікуваний результат

VideoProcessor використовує TranscriptionOrchestrator, end-to-end: upload video → ingestion → STT chain → SourceDocument

## Тестування

**Автоматизовано:**
- Unit test factory: chain + API keys → correct providers list
- Unit test factory: missing all keys → ValueError
- Unit test factory: skip provider without key (warning in log)
- Unit test VideoProcessor: mock orchestrator → SourceDocument з правильними chunks
- Unit test VideoProcessor: metadata має stt_provider, stt_model, cost, attempts_count
- Unit test: TranscriptSegment → ContentChunk маппінг (таймкоди в metadata)

**Human control:**
Upload відео через API → ingestion → перевірити processed_content + ExternalServiceCall records

## Сумісність з існуючим кодом

- **КРИТИЧНО:** поточний VideoProcessor = composite Gemini→Whisper. Повністю замінюємо логіку.
- GeminiVideoProcessor / WhisperVideoProcessor залишаються як legacy classes в video.py
- Перевірити всі місця де створюється VideoProcessor — оновити DI
- Якщо є тести з mock TranscribeFunc — оновити на mock Orchestrator
- SourceDocument.chunks: ContentChunk(chunk_type=TRANSCRIPT, metadata={"start_sec", "end_sec"}) — перевірити сумісність з MergeStep

## Checklist

- [ ] Код написаний і проходить `make check`
- [ ] Unit tests написані і зелені
- [ ] Legacy classes (Gemini/Whisper) не видалені
- [ ] DI wiring оновлений (factory + worker)
- [ ] Human control пройдений
