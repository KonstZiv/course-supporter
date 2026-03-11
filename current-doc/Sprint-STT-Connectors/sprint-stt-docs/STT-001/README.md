# STT-001: STT Provider ABC + уніфіковані моделі

**Sprint:** STT Connectors
**Оцінка:** 3h

---

## Мета

Єдиний контракт для всіх STT провайдерів і уніфіковані моделі результатів

## Що робимо

Створити пакет `src/course_supporter/stt/` з:
- `STTProvider` ABC з `transcribe()`, `health_check()`, `has_valid_config()`, `estimate_cost()` (non-abstract, default None)
- `AudioInput` (file_path + compressed_path + s3_url)
- `TranscriptSegment`, `TranscriptResult`, `TranscriptionAttempt`, `TranscriptionReport` — **Pydantic BaseModel**
- Error hierarchy: `STTError` → `STTAuthError`, `STTRateLimitError`, `STTTimeoutError`, `STTServerError`, `STTQuotaError`

**Ключовий момент:** existing pipeline дає local WAV (16kHz mono) через yt-dlp + ffmpeg. `STTProvider.transcribe()` приймає `AudioInput(file_path=...)`, не URL.

## Як робимо (коротко)

Створити `src/course_supporter/stt/__init__.py` — пакет STT
Створити `src/course_supporter/stt/base.py`:
  - `STTProvider(ABC)` з abstractmethod `transcribe()`, `health_check()`, `has_valid_config()`
  - `@property name → str` (provider identifier)
Створити `src/course_supporter/stt/models.py`:
  - Всі моделі як **Pydantic BaseModel** (не dataclass — consistency з проєктом)
  - `AudioInput(BaseModel)`: file_path (Path), compressed_path (Path | None), s3_url (str | None), content_type (str)
  - `TranscriptSegment(BaseModel)`: text, start, end, speaker, confidence, language
  - `TranscriptResult(BaseModel)`: text, segments, language_detected, languages_detected, duration_seconds, provider, model, raw_response
  - `TranscriptionAttempt(BaseModel)`: provider, model, success, duration_ms, error, error_type, cost_usd, result (optional)
  - `TranscriptionReport(BaseModel)`: result, attempts, total_duration_ms, total_cost_usd
Створити `src/course_supporter/stt/exceptions.py`:
  - `STTError(Exception)` — base, з `provider` і `error_type` fields
  - `STTAuthError` (auth) — NOT retryable, NOT fallback
  - `STTRateLimitError` (rate_limit) — fallback
  - `STTTimeoutError` (timeout) — fallback
  - `STTServerError` (5xx) — fallback
  - `STTQuotaError` (quota) — fallback

## Очікуваний результат

Пакет `stt/` з чистими абстракціями, готовий до реалізації конекторів

## Тестування

**Автоматизовано:**
- Unit test TranscriptResult: створення, серіалізація, валідація полів
- Unit test TranscriptSegment: boundary values (start=0, end=0, confidence=0/1)
- Unit test TranscriptionReport: обчислення total_cost_usd з кількох attempts
- Unit test AudioInput: file_path обов'язковий, compressed_path/s3_url optional
- Unit test exceptions: error_type property, str representation з provider name
- Type check: mypy strict на всіх нових файлах

**Human control:**
Code review — чи контракт STTProvider достатньо generic? Чи можна додати новий provider (Google, Azure) без зміни ABC?

## Сумісність з існуючим кодом

- В `heavy_steps.py` вже є `Transcript` і `TranscriptSegment` (Pydantic) — нові моделі **не замінюють** їх, а створюються в окремому пакеті `stt/`. Маппінг між ними — в STT-007.
- Error hierarchy сумісна з ExternalServiceCall (поля action, strategy, provider, error_message)
- Новий пакет `stt/` не зачіпає існуючий код

## Checklist

- [ ] Код написаний і проходить `make check`
- [ ] Unit tests написані і зелені
- [ ] Сумісність з існуючим кодом перевірена
- [ ] Human control пройдений
