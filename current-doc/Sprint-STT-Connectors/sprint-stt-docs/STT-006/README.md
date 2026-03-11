# STT-006: TranscriptionOrchestrator — fallback chain

**Sprint:** STT Connectors
**Оцінка:** 4h

---

## Мета

Orchestrator що пробує провайдерів по черзі з retry, fallback і audio preprocessing

## Що робимо

`TranscriptionOrchestrator` з:
- Configurable provider chain
- Retry policy per provider
- Error-based fallback decision
- Audio preprocessing: WAV→MP3 для великих файлів (один раз перед першим provider)
- Logging кожної спроби в `ExternalServiceCall`

## Як робимо (коротко)

Створити `src/course_supporter/stt/orchestrator.py`:
  - `class TranscriptionOrchestrator`
  - `__init__(providers, settings, service_call_repo)`
  - `async transcribe(audio: AudioInput) → TranscriptionReport`:
    1. `_maybe_compress(audio)` — WAV→MP3 якщо > threshold (один раз)
    2. For each provider: retry loop → `_try_provider()` → `_log_attempt()`
    3. Success → return `TranscriptionReport`
    4. Auth error → raise (NOT fallback)
    5. Other errors → next provider
    6. All failed → raise `STTError('all', ...)`
  - `_try_provider()`: timeout via `asyncio.wait_for`, catch STT exceptions
  - `_should_fallback(error_type)`: auth → NEVER, rest configurable
  - `_log_attempt()`: create `ExternalServiceCall` record
  - `_maybe_compress()`: ffmpeg WAV→MP3 if file > `stt_compress_threshold_mb` (default 50MB)
  - Cleanup: `try/finally` removes compressed MP3 after all attempts
  - `async health_check_all() → dict[str, bool]`

## Очікуваний результат

Orchestrator автоматично обробляє failures, компресує великі файли, переходить до наступного provider

## Тестування

**Автоматизовано:**
- Unit test: happy path — first provider succeeds
- Unit test: first fails (timeout) → second succeeds → 2 attempts
- Unit test: retry within provider — first try 5xx, second try succeeds
- Unit test: auth error → NOT fallback, raise
- Unit test: all providers fail → raise STTError('all')
- Unit test: fallback_on_errors='timeout' — only timeout fallbacks
- Unit test: _log_attempt → ExternalServiceCall created
- Unit test: _maybe_compress — file > threshold → MP3 created
- Unit test: _maybe_compress — file < threshold → no change

**Human control:**
Зупинити provider (bad API key) → перевірити fallback + ExternalServiceCall logging

## Сумісність з існуючим кодом

- `ExternalServiceCall` має потрібні поля (action, strategy, provider, model_id, cost_usd, latency_ms, success, error_message)
- `asyncio.wait_for` timeout не конфліктує з ARQ job timeout
- ffmpeg для WAV→MP3 — вже є в системі (використовується в audio extraction)

## Checklist

- [ ] Код написаний і проходить `make check`
- [ ] Unit tests написані і зелені
- [ ] Audio preprocessing працює
- [ ] Fallback logic і retry коректні
- [ ] Human control пройдений
