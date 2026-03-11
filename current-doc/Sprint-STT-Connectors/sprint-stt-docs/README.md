# Sprint STT — Контроль результатів

**Sprint:** STT Connectors — конектори транскрибації з конфігурованим fallback
**Оцінка:** 6-8 днів

---

## Задачі

- [STT-001: STT Provider ABC + уніфіковані моделі](./STT-001/README.md) (3h)
- [STT-002: STTSettings + конфігурація provider chain](./STT-002/README.md) (2h)
- [STT-003: Deepgram connector](./STT-003/README.md) (4h)
- [STT-004: Soniox connector](./STT-004/README.md) (4-6h)
- [STT-005: ElevenLabs connector](./STT-005/README.md) (4h)
- [STT-006: TranscriptionOrchestrator — fallback chain](./STT-006/README.md) (4h)
- [STT-007: Factory + VideoProcessor інтеграція](./STT-007/README.md) (4h)
- [STT-008: Порівняльний тест якості + документація результатів](./STT-008/README.md) (3h)

---

## Порядок виконання

```
STT-001 (ABC + моделі) ──→ STT-002 (Settings) ──→ STT-003 (Deepgram)  ──┐
                                                    STT-004 (Soniox)    ──┼──→ STT-006 (Orchestrator) ──→ STT-007 (Integration) ──→ STT-008 (Test)
                                                    STT-005 (ElevenLabs)──┘
```

STT-003, STT-004, STT-005 можна робити паралельно після STT-001 + STT-002.

---

## Ключові архітектурні рішення (v2)

1. **STT chain повністю замінює Gemini Vision + Whisper** як default video transcription pipeline
2. **GeminiVideoProcessor / WhisperVideoProcessor залишаються в коді** як legacy (для VisualExtractor sprint та offline fallback)
3. **VideoProcessor** стає тонкою обгорткою: extract audio → STT chain → SourceDocument
4. **Моделі — Pydantic BaseModel** (не dataclass), сумісні з існуючими моделями проєкту
5. **Audio preprocessing** WAV→MP3 для файлів >50MB — один раз в Orchestrator, до першого provider. **Cleanup** temp файлів в `try/finally`
6. **STT SDKs — optional deps** (`[stt]` extra в pyproject.toml), install: `uv sync --extra stt`
7. **`has_valid_config()`** — обов'язковий метод в STTProvider ABC

---

## Автоматизований контроль

- `make check` (ruff + mypy + pytest) зелений на кожному PR
- Unit tests для кожного provider: mock response → TranscriptResult
- Unit tests для Orchestrator: fallback chain, retry logic, error classification
- Integration test: VideoProcessor → Orchestrator → SourceDocument

## Ручний контроль (Human testing)

- **STT-003/004/005:** кожен конектор тестується на реальному 10-хв українському аудіо
- **STT-006:** перевірка logging і fallback при вимкненому provider
- **STT-007:** end-to-end через API (upload video → ingestion → STT → processed_content)
- **STT-008:** side-by-side порівняння трьох провайдерів, вибір оптимальної chain

## Сумісність з існуючим кодом

> **Existing audio pipeline (не змінюється):**
> YouTube URL → yt-dlp → local WAV → ffmpeg 16kHz mono → **local WAV файл**
>
> STT providers отримують цей WAV через AudioInput(file_path=...).
> Orchestrator автоматично компресує файли >50MB у MP3 перед відправкою. Temp файли (WAV, MP3) видаляються в `try/finally`.
>
> **GeminiVideoProcessor / WhisperVideoProcessor** залишаються як окремі класи.
> Не використовуються в default pipeline, але не видаляються — для VisualExtractor sprint.

> **Перед початком кожної задачі:**
> 1. Перевірити поточний стан VideoProcessor, TranscribeFunc, ExternalServiceCall
> 2. Перевірити що зміни не ламають існуючі тести
> 3. Якщо є мок TranscribeFunc в тестах — оновити на мок Orchestrator (в STT-007)

## Definition of Done

- [ ] Три конектори працюють з реальними API
- [ ] STT_PROVIDER_CHAIN керує порядком (через env)
- [ ] Fallback працює (timeout/5xx → наступний provider)
- [ ] Auth error НЕ тригерить fallback
- [ ] Audio preprocessing: великі WAV → MP3 автоматично
- [ ] Кожна спроба в ExternalServiceCall
- [ ] VideoProcessor використовує Orchestrator (Gemini/Whisper — legacy, не в default)
- [ ] Порівняльний тест проведений, chain обрана
- [ ] `.env.prod` оновлений
- [ ] `make check` зелений
