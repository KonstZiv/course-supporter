# STT-003: Deepgram connector

**Sprint:** STT Connectors
**Оцінка:** 4h

---

## Мета

Працюючий конектор до Deepgram Nova-3 batch API

## Що робимо

Реалізувати `DeepgramProvider(STTProvider)` з маппінгом response → `TranscriptResult`. Binary upload preferred (не потрібен S3).

## Як робимо (коротко)

Додати `deepgram-sdk>=4.0` в optional deps (`[stt]` extra)
Створити `src/course_supporter/stt/providers/__init__.py`
Створити `src/course_supporter/stt/providers/deepgram.py`:
  - `class DeepgramProvider(STTProvider)`
  - `__init__(api_key, model, tier, smart_format, diarize, keyterms)`
  - `has_valid_config() → bool` (чи є api_key)
  - `transcribe(audio: AudioInput, ...)`:
    1. `audio_bytes = audio.best_path.read_bytes()` — binary upload preferred
    2. `PrerecordedOptions(model, language, smart_format, diarize, punctuate, keywords)`
    3. `client.listen.asyncrest.v('1').transcribe_file(buffer, options)`
    4. Group words by speaker/pauses → `TranscriptSegment`
    5. Error mapping: 401→STTAuthError, 429→STTRateLimitError, 500→STTServerError
  - `health_check()`: GET /v1/projects
  - `_estimate_cost(duration_seconds)`: batch $0.0043/min

## Очікуваний результат

DeepgramProvider транскрибує аудіо через API і повертає TranscriptResult з сегментами, таймкодами і speaker labels

## Тестування

**Автоматизовано:**
- Unit test з mock: Deepgram JSON → TranscriptResult
- Unit test: words → segments grouping (by speaker, by pauses > 2s)
- Unit test: cost estimation — 120 sec → $0.0086
- Unit test: error mapping — 401, 429, 500, timeout
- Unit test: has_valid_config() — empty key → False
- Integration test (manual): 30-сек аудіо → TranscriptResult

**Human control:**
10-хв українське аудіо: текст, таймкоди, speaker labels, keyterms recognition

## Сумісність з існуючим кодом

- `deepgram-sdk` v4+ сумісний з Python 3.13+ і asyncio
- Binary upload (`transcribe_file`) — preferred, S3 не потрібен
- WAV 16kHz mono приймається Deepgram без конвертації
- `AudioInput.best_path` — повертає MP3 якщо Orchestrator компресував, інакше WAV

## Checklist

- [ ] Код написаний і проходить `make check`
- [ ] Unit tests написані і зелені
- [ ] Сумісність з існуючим кодом перевірена
- [ ] Human control пройдений
