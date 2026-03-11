# STT-005: ElevenLabs connector

**Sprint:** STT Connectors
**Оцінка:** 4h

---

## Мета

Працюючий конектор до ElevenLabs Scribe v2 з keyterm prompting

## Що робимо

Реалізувати `ElevenLabsProvider(STTProvider)` з keyterms, ISO 639-3 mapping, можливо sync SDK → `asyncio.to_thread()`

## Як робимо (коротко)

Додати `elevenlabs>=2.0` в `[stt]` extra
Створити `src/course_supporter/stt/providers/elevenlabs.py`:
  - `class ElevenLabsProvider(STTProvider)`
  - `__init__(api_key, model, diarize, tag_audio_events, keyterms)`
  - `has_valid_config() → bool`
  - `transcribe(audio: AudioInput, ...)`:
    1. File upload (multipart) — preferred, S3 не потрібен
    2. **ISO 639-3 mapping:** 'uk' → 'ukr', 'ru' → 'rus', 'en' → 'eng'
    3. Keyterms: до 100 terms для покращення розпізнавання
    4. `diarize=True`: до 48 спікерів
    5. SDK може бути sync → `asyncio.to_thread()`
    6. Words → segments grouping
  - `health_check()`: GET /v1/models → scribe_v2 доступний
  - `_estimate_cost(duration_seconds)`: ~$0.0067/min

## Очікуваний результат

ElevenLabsProvider транскрибує з keyterm prompting для технічної термінології

## Тестування

**Автоматизовано:**
- Unit test з mock: ElevenLabs response → TranscriptResult
- Unit test: language code mapping 'uk'→'ukr', 'ru'→'rus'
- Unit test: keyterms (max 100, truncate)
- Unit test: cost estimation, error mapping

**Human control:**
10-хв аудіо з keyterms=['Python','Django','ORM']. Порівняти з Deepgram і Soniox.

## Сумісність з існуючим кодом

- ISO 639-3 vs 639-1: маппінг потрібен, решта системи використовує 639-1
- SDK може бути sync → `asyncio.to_thread()` wrapper
- `AudioInput.best_path` для file upload

## Checklist

- [ ] Код написаний і проходить `make check`
- [ ] Unit tests написані і зелені
- [ ] ISO 639-3 mapping працює
- [ ] Human control пройдений
