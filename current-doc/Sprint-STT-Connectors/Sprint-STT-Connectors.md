# Sprint STT — Конектори транскрибації з конфігурованим fallback

**Статус:** DRAFT v2
**Оцінка:** 6-8 днів
**Залежність:** Sprint 2, Epic 4 (Heavy Steps Extraction)
**Ціль:** Три STT-провайдери (Deepgram, Soniox, ElevenLabs) як injectable connectors з конфігурованим пріоритетом, fallback chain, та збереженням метрик. Повна заміна Gemini Vision + Whisper як default video transcription pipeline.

---

## Контекст

### Поточний стан

VideoProcessor використовує **два підходи**:
1. **GeminiVideoProcessor** (primary) — завантажує відео в Gemini File API, vision model генерує транскрипт. Проблеми: повільно, дорого, непередбачувана якість для української, галюцинації.
2. **WhisperVideoProcessor** (fallback) — локальний Whisper на CPU. Проблеми: повільно (realtime або гірше на 2-core VPS), низька якість для української.

### Архітектурне рішення

**STT chain повністю замінює обидва підходи** для транскрипції аудіо:

- `GeminiVideoProcessor` **залишається в коді** як legacy — не видаляємо, бо це працюючий Vision-based підхід, який може знадобитись для інших задач (наприклад, аналіз візуального контенту).
- `WhisperVideoProcessor` **залишається в коді** як legacy/offline fallback.
- **Default pipeline:** audio extraction (yt-dlp + ffmpeg) → STT provider chain → SourceDocument.
- **Наступний sprint (VisualExtractor):** окремий pipeline step для аналізу візуального контенту (слайди, код на екрані). Архітектурно аналогічний STT chain (ABC → провайдери → chain). Merge з транскриптом по таймкодах. Архітектура `ContentChunk` з типами (TRANSCRIPT, SLIDE_DESCRIPTION) вже це підтримує.

Sprint 2 AR-5 визначив injectable contract:

```python
TranscribeFunc = Callable[[str, TranscribeParams], Awaitable[Transcript]]
```

Цей спрінт замінює `TranscribeFunc` на `TranscriptionOrchestrator` з provider chain.

---

## Архітектурне рішення: STT Provider Chain

### Принцип

```
Existing pipeline (не змінюється):
  YouTube URL → yt-dlp --extract-audio → local WAV → ffmpeg 16kHz mono → local WAV

Новий STT layer:
  Local WAV → AudioInput(file_path=...) → TranscriptionOrchestrator → TranscriptionReport

  Orchestrator:
    1. Preprocessing: WAV→MP3 для великих файлів (>100MB) — один раз, до першого provider
    2. Пробує провайдерів по черзі:
       Provider A (binary upload) → читає bytes з file_path → відправляє напряму
       Provider B (URL only) → upload в S3 → presigned URL → відправляє URL

Settings визначає:
  1. Ordered list провайдерів (priority chain)
  2. Per-provider конфігурацію (api key, model, language hints)
  3. Fallback policy (retry count, timeout, which errors trigger fallback)

Runtime:
  TranscriptionOrchestrator пробує провайдерів по черзі.
  Перший успішний результат — повертається.
  Кожна спроба логується в ExternalServiceCall.
```

### VideoProcessor — нова архітектура

```python
class VideoProcessor(SourceProcessor):
    """Extract audio from video → STT provider chain → SourceDocument."""

    def __init__(self, orchestrator: TranscriptionOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def process(self, source: MaterialEntry, *, router: ModelRouter | None = None) -> SourceDocument:
        # 1. Extract audio (existing yt-dlp + ffmpeg pipeline)
        audio_path = await self._extract_audio(source.source_url)

        # 2. Transcribe via STT chain
        audio = AudioInput(file_path=audio_path)
        report = await self._orchestrator.transcribe(audio)

        # 3. Convert to SourceDocument
        return self._to_source_document(source, report)
```

GeminiVideoProcessor і WhisperVideoProcessor залишаються як окремі класи, але **не використовуються** в default pipeline. Їх можна активувати через конфігурацію для специфічних кейсів.

### Audio Delivery — як аудіо потрапляє до провайдера

Existing pipeline вже дає **local WAV файл (16kHz mono)**. Кожен STT provider приймає аудіо по-різному:

| Provider | Binary upload (bytes) | URL | Preferred mode |
|---|---|---|---|
| **Deepgram** | transcribe_file() | transcribe_url() | Binary — не потрібен S3 |
| **Soniox** | перевірити SDK | перевірити API | З'ясувати першим кроком |
| **ElevenLabs** | multipart upload | audio_url param | Binary — не потрібен S3 |

**Preprocessing для великих файлів:** 2hr WAV = ~700MB. Рішення:
- Orchestrator перевіряє розмір `AudioInput.file_path`
- Якщо > 50MB (configurable): конвертує WAV → MP3 **один раз** перед першим provider: `ffmpeg -i input.wav -acodec libmp3lame -q:a 4 output.mp3`
- Зберігає MP3 path в `AudioInput.compressed_path`
- Кожен provider використовує `audio.best_path` (compressed якщо є, інакше original)
- **Cleanup:** Orchestrator видаляє compressed MP3 в `try/finally` після всіх спроб. VideoProcessor видаляє extracted WAV.

### Конфігурація через env

```python
class STTSettings(BaseSettings):
    """STT provider chain configuration."""

    # -- Provider priority (ordered, comma-separated) --
    stt_provider_chain: str = "deepgram,soniox,elevenlabs"

    # -- Language hints --
    stt_default_language: str = "uk"            # ISO 639-1
    stt_language_detection: bool = False
    stt_additional_languages: str = ""           # "ru,en"

    # -- Deepgram --
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    deepgram_tier: str = "batch"
    deepgram_smart_format: bool = True
    deepgram_diarize: bool = True
    deepgram_punctuate: bool = True
    deepgram_keyterms: str = ""                 # "Python,Django,FastAPI"

    # -- Soniox --
    soniox_api_key: str = ""
    soniox_model: str = "soniox-v3"
    soniox_enable_translation: bool = False
    soniox_translation_target: str = ""

    # -- ElevenLabs --
    elevenlabs_api_key: str = ""
    elevenlabs_model: str = "scribe_v2"
    elevenlabs_diarize: bool = True
    elevenlabs_tag_audio_events: bool = False
    elevenlabs_keyterms: str = ""               # up to 100 terms

    # -- Fallback policy --
    stt_max_retries_per_provider: int = 2
    stt_provider_timeout: int = 600             # seconds (10 min for 2hr audio)
    stt_fallback_on_errors: str = "timeout,5xx,rate_limit"

    # -- Audio preprocessing --
    stt_compress_threshold_mb: int = 50        # WAV→MP3 if file > this size

    model_config = SettingsConfigDict(env_prefix="", env_file=".env")
```

### .env приклад

```bash
# -- STT Provider Chain --
STT_PROVIDER_CHAIN=deepgram,soniox,elevenlabs
STT_DEFAULT_LANGUAGE=uk
STT_ADDITIONAL_LANGUAGES=ru,en

# -- Deepgram --
DEEPGRAM_API_KEY=dg_live_...
DEEPGRAM_MODEL=nova-3
DEEPGRAM_KEYTERMS=Python,Django,FastAPI,PostgreSQL

# -- Soniox --
SONIOX_API_KEY=sx_...
SONIOX_MODEL=soniox-v3

# -- ElevenLabs --
ELEVENLABS_API_KEY=el_...
ELEVENLABS_MODEL=scribe_v2
ELEVENLABS_KEYTERMS=Python,Django,ORM,PostgreSQL

# -- Fallback --
STT_MAX_RETRIES_PER_PROVIDER=2
STT_PROVIDER_TIMEOUT=600
STT_FALLBACK_ON_ERRORS=timeout,5xx,rate_limit
STT_COMPRESS_THRESHOLD_MB=50
```

---

## Моделі

### ВАЖЛИВО: Pydantic BaseModel, не dataclass

Весь проєкт використовує Pydantic `BaseModel` для data models. В `heavy_steps.py` вже є `Transcript` і `TranscriptSegment` — нові моделі **розширюють** цей підхід, не дублюють.

### Уніфікований результат транскрибації

```python
class TranscriptSegment(BaseModel):
    """Single transcript segment with timestamps."""
    text: str
    start: float              # seconds from beginning
    end: float                # seconds from beginning
    speaker: str | None = None       # "speaker_0", "speaker_1", ...
    confidence: float | None = None  # 0.0-1.0
    language: str | None = None      # detected language for this segment

class TranscriptResult(BaseModel):
    """Unified result from any STT provider."""
    text: str                           # full text
    segments: list[TranscriptSegment]   # segments with timestamps
    language_detected: str | None = None       # primary language
    languages_detected: list[str] = []  # all detected languages
    duration_seconds: float             # audio duration
    provider: str                       # "deepgram" | "soniox" | "elevenlabs"
    model: str                          # "nova-3" | "soniox-v3" | "scribe_v2"
    raw_response: dict[str, Any] | None = None  # original response (debug)

class TranscriptionAttempt(BaseModel):
    """Single transcription attempt (for logging and reporting)."""
    provider: str
    model: str
    success: bool
    duration_ms: int
    error: str | None = None
    error_type: str | None = None       # "auth", "timeout", "5xx", "rate_limit", "quota"
    cost_usd: float | None = None
    result: TranscriptResult | None = None  # only if success

class TranscriptionReport(BaseModel):
    """Full report with fallback chain results."""
    result: TranscriptResult            # successful result
    attempts: list[TranscriptionAttempt] # all attempts (including failed)
    total_duration_ms: int              # total time of all attempts
    total_cost_usd: float               # total cost of all attempts
```

### STT Provider ABC

```python
from abc import ABC, abstractmethod

class STTProvider(ABC):
    """Base class for all STT connectors."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier: 'deepgram', 'soniox', 'elevenlabs'."""
        ...

    @abstractmethod
    async def transcribe(
        self,
        audio: AudioInput,
        *,
        language: str = "uk",
        additional_languages: list[str] | None = None,
        keyterms: list[str] | None = None,
        diarize: bool = True,
    ) -> TranscriptResult:
        """Transcribe audio file.

        AudioInput.file_path is always populated (local WAV 16kHz mono).
        AudioInput.compressed_path may be populated (MP3 for large files).
        Provider decides how to deliver audio:
        - binary upload: read bytes from file_path/compressed_path
        - URL: use audio.s3_url (if available) or upload to S3

        Raises:
            STTAuthError: invalid API key (NOT fallback)
            STTRateLimitError: rate limit exceeded
            STTTimeoutError: provider timeout
            STTServerError: 5xx from provider
            STTError: other errors
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check provider availability."""
        ...

    @abstractmethod
    def has_valid_config(self) -> bool:
        """Check if provider has required configuration (API key etc)."""
        ...

    def estimate_cost(self, duration_seconds: float) -> float | None:
        """Estimate transcription cost in USD. Override in concrete providers."""
        return None
```

### Error hierarchy

```python
class STTError(Exception):
    """Base STT error."""
    error_type: str = "unknown"  # class variable — subclasses override

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")

class STTAuthError(STTError):
    """401/403 — NOT retryable, NOT fallback (config error)."""
    error_type = "auth"

class STTRateLimitError(STTError):
    """429 — retryable, fallback."""
    error_type = "rate_limit"

class STTTimeoutError(STTError):
    """Timeout — fallback."""
    error_type = "timeout"

class STTServerError(STTError):
    """5xx — fallback."""
    error_type = "5xx"

class STTQuotaError(STTError):
    """Quota exceeded — fallback."""
    error_type = "quota"
```

---

## Orchestrator

```python
class TranscriptionOrchestrator:
    """Try providers in order according to configured chain."""

    def __init__(
        self,
        providers: list[STTProvider],
        settings: STTSettings,
        service_call_repo: ExternalServiceCallRepository,
    ) -> None:
        self._providers = providers
        self._settings = settings
        self._repo = service_call_repo
        self._fallback_errors = self._parse_fallback_errors(settings.stt_fallback_on_errors)

    async def transcribe(self, audio: AudioInput) -> TranscriptionReport:
        # Preprocessing: compress large files once
        audio = await self._maybe_compress(audio)

        try:
            return await self._run_chain(audio)
        finally:
            # Cleanup: remove compressed MP3 if we created it
            if audio.compressed_path and audio.compressed_path.exists():
                audio.compressed_path.unlink(missing_ok=True)

    async def _run_chain(self, audio: AudioInput) -> TranscriptionReport:
        attempts: list[TranscriptionAttempt] = []

        for provider in self._providers:
            for retry in range(self._settings.stt_max_retries_per_provider):
                attempt = await self._try_provider(provider, audio)
                attempts.append(attempt)
                await self._log_attempt(attempt)

                if attempt.success:
                    return TranscriptionReport(
                        result=attempt.result,
                        attempts=attempts,
                        total_duration_ms=sum(a.duration_ms for a in attempts),
                        total_cost_usd=sum(a.cost_usd or 0 for a in attempts),
                    )

                if not self._should_fallback(attempt.error_type):
                    raise STTAuthError(provider.name, attempt.error or "Config error")

            # All retries for this provider exhausted — next

        # All providers failed
        raise STTError("all", f"All {len(self._providers)} providers failed. Attempts: {len(attempts)}")

    async def _maybe_compress(self, audio: AudioInput) -> AudioInput:
        """Compress WAV→MP3 if file exceeds threshold. One-time operation."""
        threshold = self._settings.stt_compress_threshold_mb * 1024 * 1024
        if audio.file_path.stat().st_size > threshold:
            mp3_path = audio.file_path.with_suffix(".mp3")
            await _ffmpeg_compress(audio.file_path, mp3_path)
            return audio.model_copy(update={"compressed_path": mp3_path})
        return audio

    def _should_fallback(self, error_type: str | None) -> bool:
        if error_type == "auth":
            return False  # NEVER fallback on auth errors
        if "all" in self._fallback_errors:
            return True
        return error_type in self._fallback_errors if error_type else False
```

### Factory

```python
def create_stt_providers(settings: STTSettings) -> list[STTProvider]:
    """Create ordered list of providers from settings."""
    registry: dict[str, Callable[[], STTProvider]] = {
        "deepgram": lambda: DeepgramProvider(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_model,
            tier=settings.deepgram_tier,
            smart_format=settings.deepgram_smart_format,
            diarize=settings.deepgram_diarize,
            keyterms=_parse_csv(settings.deepgram_keyterms),
        ),
        "soniox": lambda: SonioxProvider(
            api_key=settings.soniox_api_key,
            model=settings.soniox_model,
        ),
        "elevenlabs": lambda: ElevenLabsProvider(
            api_key=settings.elevenlabs_api_key,
            model=settings.elevenlabs_model,
            diarize=settings.elevenlabs_diarize,
            tag_audio_events=settings.elevenlabs_tag_audio_events,
            keyterms=_parse_csv(settings.elevenlabs_keyterms),
        ),
    }

    chain = [name.strip() for name in settings.stt_provider_chain.split(",")]
    providers: list[STTProvider] = []
    for name in chain:
        if name not in registry:
            raise ValueError(f"Unknown STT provider: {name}")
        provider = registry[name]()
        if not provider.has_valid_config():
            logger.warning("stt_provider_skipped", provider=name, reason="missing API key")
            continue
        providers.append(provider)

    if not providers:
        raise ValueError("No valid STT providers configured")

    return providers


def create_transcription_orchestrator(
    settings: Settings,
    service_call_repo: ExternalServiceCallRepository,
) -> TranscriptionOrchestrator:
    """Top-level factory for DI."""
    providers = create_stt_providers(settings.stt)
    return TranscriptionOrchestrator(
        providers=providers,
        settings=settings.stt,
        service_call_repo=service_call_repo,
    )
```

---

## Інтеграція з існуючою архітектурою

### VideoProcessor — повний редизайн

```python
# БУЛО (composite з Gemini + Whisper fallback):
class VideoProcessor(SourceProcessor):
    def __init__(self, *, enable_whisper: bool = True, transcribe_func: TranscribeFunc | None = None):
        self._gemini = GeminiVideoProcessor()
        self._whisper = WhisperVideoProcessor(...) if enable_whisper else None

    async def process(self, source, *, router=None):
        try:
            return await self._gemini.process(source, router=router)
        except Exception:
            if self._whisper:
                return await self._whisper.process(source)
            raise

# СТАЄ (thin wrapper around STT chain):
class VideoProcessor(SourceProcessor):
    def __init__(self, orchestrator: TranscriptionOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def process(self, source: MaterialEntry, *, router: ModelRouter | None = None) -> SourceDocument:
        # Existing pipeline: extract audio
        audio_path = await self._extract_audio(source.source_url)
        # STT chain
        audio = AudioInput(file_path=audio_path)
        report = await self._orchestrator.transcribe(audio)
        # Convert to SourceDocument
        return self._to_source_document(source, report)

    def _to_source_document(self, source: MaterialEntry, report: TranscriptionReport) -> SourceDocument:
        chunks = [
            ContentChunk(
                chunk_type=ChunkType.TRANSCRIPT,
                text=seg.text,
                index=i,
                metadata={
                    "start_sec": seg.start,
                    "end_sec": seg.end,
                    "speaker": seg.speaker,
                    "confidence": seg.confidence,
                },
            )
            for i, seg in enumerate(report.result.segments)
        ]
        return SourceDocument(
            source_type=SourceType.VIDEO,
            source_url=source.source_url,
            title=source.name or "",
            chunks=chunks,
            metadata={
                "stt_provider": report.result.provider,
                "stt_model": report.result.model,
                "duration_seconds": report.result.duration_seconds,
                "languages_detected": report.result.languages_detected,
                "attempts_count": len(report.attempts),
                "total_cost_usd": report.total_cost_usd,
            },
        )
```

### GeminiVideoProcessor / WhisperVideoProcessor — legacy

Обидва класи **залишаються в коді** (video.py) без змін. Не використовуються в default pipeline, але доступні для:
- **GeminiVideoProcessor** — потенційно для VisualExtractor sprint (аналіз візуального контенту)
- **WhisperVideoProcessor** — offline fallback без інтернету, або для тестування

### Factory / DI wiring

```python
# БУЛО (ingestion/factory.py):
def create_processors(heavy: HeavySteps) -> dict[SourceType, SourceProcessor]:
    return {
        SourceType.VIDEO: VideoProcessor(transcribe_func=heavy.transcribe),
        ...
    }

# СТАЄ:
def create_processors(
    heavy: HeavySteps,
    orchestrator: TranscriptionOrchestrator | None = None,
) -> dict[SourceType, SourceProcessor]:
    if orchestrator is None:
        raise ValueError("TranscriptionOrchestrator required for video processing")
    return {
        SourceType.VIDEO: VideoProcessor(orchestrator=orchestrator),
        ...
    }
```

### ARQ Worker — ingestion callback

ExternalServiceCall записи створюються автоматично Orchestrator-ом для кожної спроби:

```python
# Orchestrator._log_attempt():
await service_call_repo.create(
    action="transcription",
    strategy=f"stt:{attempt.provider}",
    provider=attempt.provider,
    model_id=attempt.model,
    latency_ms=attempt.duration_ms,
    cost_usd=attempt.cost_usd,
    success=attempt.success,
    error_message=attempt.error,
)
```

---

## Залежності (pyproject.toml)

```toml
[project.optional-dependencies]
stt = [
    "deepgram-sdk>=4.0",
    "soniox>=1.0",        # verify PyPI package name
    "elevenlabs>=2.0",
]
```

**STT SDKs як optional deps** (`[stt]` extra), не core. Install: `uv sync --extra stt`.
Якщо Soniox SDK не існує або нестабільний — fallback на REST API через httpx (вже є в deps).

---

## Сценарії використання

### Сценарій 1: Стандартний (production)

```bash
STT_PROVIDER_CHAIN=deepgram,soniox,elevenlabs
```

Deepgram — найшвидший і найдешевший. Якщо зафейлить (timeout, 5xx) — Soniox. Якщо і він — ElevenLabs.

### Сценарій 2: Максимальна якість для української

```bash
STT_PROVIDER_CHAIN=soniox
STT_DEFAULT_LANGUAGE=uk
STT_ADDITIONAL_LANGUAGES=ru,en
```

Soniox single provider — code-switching для суржику, auto-detect мов.

### Сценарій 3: Тестування / порівняння

```bash
STT_PROVIDER_CHAIN=deepgram
# Потім:
STT_PROVIDER_CHAIN=soniox
# Потім:
STT_PROVIDER_CHAIN=elevenlabs
```

### Сценарій 4: Технічні лекції з термінологією

```bash
STT_PROVIDER_CHAIN=elevenlabs,deepgram
ELEVENLABS_KEYTERMS=Python,Django,ORM,REST,API,PostgreSQL
DEEPGRAM_KEYTERMS=Python,Django,ORM,REST,API,PostgreSQL
```

---

## Епік та задачі

### Task 1: STT Provider ABC + моделі (3h)

- `STTProvider` ABC з `transcribe()`, `health_check()`, `has_valid_config()`
- `TranscriptResult`, `TranscriptSegment`, `TranscriptionAttempt`, `TranscriptionReport` (Pydantic BaseModel)
- `AudioInput` з `file_path`, `compressed_path`, `s3_url`
- Error hierarchy: `STTError` → `STTAuthError`, `STTRateLimitError`, `STTTimeoutError`, `STTServerError`, `STTQuotaError`
- Unit tests: моделі, error types

**Human control:** code review — чи контракт достатньо generic для будь-якого майбутнього provider

### Task 2: STTSettings + конфігурація (2h)

- `STTSettings` в Settings з усіма env variables
- `.env.example` оновлення
- Parsing chain, CSV keyterms, fallback errors
- `stt_compress_threshold_mb` для audio preprocessing
- Unit tests: parsing, defaults, validation

**Human control:** перевірити що `.env.example` зрозумілий

### Task 3: Deepgram connector (4h)

- `DeepgramProvider(STTProvider)`
- Batch mode, binary upload preferred
- Mapping response → `TranscriptResult`
- Integration test з реальним API

**Human control:** протестувати на реальному 10-хв українському аудіо

### Task 4: Soniox connector (4-6h)

- `SonioxProvider(STTProvider)`
- Code-switching uk↔ru↔en
- SDK або REST API через httpx (з'ясувати першим кроком)
- Integration test з реальним API

**Human control:** порівняти суржик handling з Deepgram

### Task 5: ElevenLabs connector (4h)

- `ElevenLabsProvider(STTProvider)`
- Scribe v2, keyterm prompting, ISO 639-3 mapping
- Integration test з реальним API

**Human control:** порівняти з двома попередніми

### Task 6: TranscriptionOrchestrator (4h)

- Orchestrator з fallback chain + retry logic
- Audio preprocessing (WAV→MP3 для великих файлів)
- Logging кожної спроби в ExternalServiceCall
- Unit tests: chain з mock providers

**Human control:** перевірити logging і fallback при зупиненому provider

### Task 7: Factory + VideoProcessor інтеграція (4h)

- Factory functions для DI
- **Повний редизайн VideoProcessor**: Gemini+Whisper composite → thin STT wrapper
- `_extract_audio()` залишається (yt-dlp + ffmpeg pipeline)
- GeminiVideoProcessor / WhisperVideoProcessor залишаються як legacy
- Оновити factory.py, worker DI wiring
- Integration test: end-to-end video → STT → SourceDocument

**Human control:** завантажити відео через API, перевірити end-to-end

### Task 8: Порівняльний тест якості (3h)

- Скрипт `scripts/stt_compare.py`
- Тест на реальному 10-15 хв українському аудіо
- Side-by-side порівняння, метрики
- Документація вибору в `docs/stt-evaluation.md`
- `.env.prod` з фінальною chain

**Human control:** прочитати всі транскрипти, оцінити якість, вибрати chain

---

## Definition of Done

- [ ] Три STT конектори працюють з реальними API
- [ ] `STT_PROVIDER_CHAIN` env variable керує порядком провайдерів
- [ ] Fallback працює: якщо primary зафейлить → автоматично наступний
- [ ] `STTAuthError` (невалідний ключ) НЕ тригерить fallback
- [ ] Кожна спроба логується в ExternalServiceCall (provider, model, cost, duration, success)
- [ ] Audio preprocessing: великі WAV файли автоматично компресуються в MP3
- [ ] Keyterm prompting працює для Deepgram і ElevenLabs
- [ ] Language hints `uk` + `ru,en` передаються всім провайдерам
- [ ] VideoProcessor використовує Orchestrator (Gemini/Whisper — legacy, не в default pipeline)
- [ ] Порівняльний тест на реальному українському аудіо проведений
- [ ] Оптимальна chain задокументована
- [ ] `make check` зелений
- [ ] `.env.example` оновлений з усіма STT змінними
- [ ] STT SDKs — optional deps (`uv sync --extra stt`)
