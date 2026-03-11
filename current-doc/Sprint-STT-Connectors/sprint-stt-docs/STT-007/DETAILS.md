# STT-007: Factory + VideoProcessor інтеграція — Деталі для виконавця

**Sprint:** STT Connectors
**Оцінка:** 4h

---

## Мета

Підключити Orchestrator до VideoProcessor через DI. Повний редизайн VideoProcessor.

## Контекст

Ця задача є частиною Sprint STT (6-8 днів).

**Ключове архітектурне рішення:**
- STT chain **повністю замінює** Gemini Vision + Whisper як default video transcription
- `GeminiVideoProcessor` — це Vision-based підхід (аналізує відео візуально), не STT. Залишається для майбутнього VisualExtractor sprint.
- `WhisperVideoProcessor` — локальний STT на CPU. Залишається як offline fallback.
- Обидва legacy classes **не видаляються**, але **не використовуються** в default pipeline.

## Залежності

**Попередня задача:** [STT-006: TranscriptionOrchestrator](../STT-006/README.md)
**Наступна задача:** [STT-008: Порівняльний тест](../STT-008/README.md)

---

## Детальний план реалізації

### 1. `src/course_supporter/stt/factory.py`

```python
import structlog

from course_supporter.stt.base import STTProvider
from course_supporter.stt.orchestrator import TranscriptionOrchestrator
from course_supporter.stt.settings import STTSettings

logger = structlog.get_logger()


def _parse_csv(raw: str) -> list[str]:
    """Parse comma-separated string into list."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def create_stt_providers(settings: STTSettings) -> list[STTProvider]:
    """Create ordered list of STT providers from settings.

    Skips providers without valid config (missing API key) with a warning.
    Raises ValueError if no valid providers configured.
    """
    from course_supporter.stt.providers.deepgram import DeepgramProvider
    from course_supporter.stt.providers.elevenlabs import ElevenLabsProvider
    from course_supporter.stt.providers.soniox import SonioxProvider

    registry: dict[str, callable] = {
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

    providers: list[STTProvider] = []
    for name in settings.provider_chain:
        if name not in registry:
            msg = f"Unknown STT provider: {name}"
            raise ValueError(msg)
        provider = registry[name]()
        if not provider.has_valid_config():
            logger.warning("stt_provider_skipped", provider=name, reason="missing API key")
            continue
        providers.append(provider)
        logger.info("stt_provider_registered", provider=name)

    if not providers:
        msg = "No valid STT providers configured. Check API keys in .env"
        raise ValueError(msg)

    logger.info("stt_chain_configured", providers=[p.name for p in providers])
    return providers


def create_transcription_orchestrator(
    settings,  # Settings (main config)
    service_call_repo,  # ExternalServiceCallRepository
) -> TranscriptionOrchestrator:
    """Top-level factory for DI. Creates orchestrator with configured provider chain."""
    providers = create_stt_providers(settings.stt)
    return TranscriptionOrchestrator(
        providers=providers,
        settings=settings.stt,
        service_call_repo=service_call_repo,
    )
```

### 2. Модифікація VideoProcessor (ingestion/video.py)

**Поточний стан (composite Gemini + Whisper):**
```python
class VideoProcessor(SourceProcessor):
    def __init__(self, *, enable_whisper=True, transcribe_func=None):
        self._gemini = GeminiVideoProcessor()
        self._whisper = WhisperVideoProcessor(...) if enable_whisper else None

    async def process(self, source, *, router=None):
        try:
            return await self._gemini.process(source, router=router)
        except Exception:
            if self._whisper:
                return await self._whisper.process(source)
            raise
```

**Новий стан (thin STT wrapper):**
```python
class VideoProcessor(SourceProcessor):
    """Extract audio from video → STT provider chain → SourceDocument.

    Uses TranscriptionOrchestrator for transcription instead of
    Gemini Vision or local Whisper.

    Legacy processors (GeminiVideoProcessor, WhisperVideoProcessor)
    remain available as separate classes for specific use cases.
    """

    def __init__(self, orchestrator: TranscriptionOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def process(
        self,
        source: MaterialEntry,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        # Step 1: Extract audio from video (existing yt-dlp + ffmpeg pipeline)
        audio_path = await self._extract_audio(source.source_url)

        try:
            # Step 2: Transcribe via STT chain
            audio = AudioInput(file_path=audio_path)
            report = await self._orchestrator.transcribe(audio)

            # Step 3: Convert to SourceDocument
            return self._to_source_document(source, report)
        finally:
            # Cleanup: remove extracted WAV to prevent disk space leak
            # (Orchestrator handles its own compressed MP3 cleanup)
            if audio_path.exists():
                audio_path.unlink(missing_ok=True)

    async def _extract_audio(self, source_url: str) -> Path:
        """Extract audio from video URL.

        Reuse existing pipeline:
        - HTTP/HTTPS URLs: yt-dlp --extract-audio → local WAV
        - Local files: ffmpeg → 16kHz mono WAV

        This method already exists in WhisperVideoProcessor._extract_audio().
        Refactor: extract as standalone function or reuse.
        """
        # Existing logic from WhisperVideoProcessor — adapt or extract
        ...

    def _to_source_document(
        self,
        source: MaterialEntry,
        report: TranscriptionReport,
    ) -> SourceDocument:
        """Convert TranscriptionReport → SourceDocument with TRANSCRIPT chunks."""
        chunks = [
            ContentChunk(
                chunk_type=ChunkType.TRANSCRIPT,
                text=segment.text,
                index=i,
                metadata={
                    "start_sec": segment.start,
                    "end_sec": segment.end,
                    "speaker": segment.speaker,
                    "confidence": segment.confidence,
                    "language": segment.language,
                },
            )
            for i, segment in enumerate(report.result.segments)
        ]

        return SourceDocument(
            source_type=SourceType.VIDEO,
            source_url=source.source_url,
            title=source.name or "",
            chunks=chunks,
            metadata={
                "strategy": f"stt:{report.result.provider}",
                "stt_provider": report.result.provider,
                "stt_model": report.result.model,
                "duration_seconds": report.result.duration_seconds,
                "languages_detected": report.result.languages_detected,
                "attempts_count": len(report.attempts),
                "total_cost_usd": report.total_cost_usd,
                "total_duration_ms": report.total_duration_ms,
            },
        )
```

### 3. Legacy classes — що з ними

**GeminiVideoProcessor** — залишається без змін в `video.py`. Не використовується в default pipeline. Потенційно для VisualExtractor sprint (аналіз візуального контенту відео).

**WhisperVideoProcessor** — залишається без змін. Offline fallback. Може використовуватись через пряму інстанціацію.

**`_extract_audio()` refactoring:** В WhisperVideoProcessor вже є `_extract_audio()` (yt-dlp + ffmpeg). Два варіанти:
- **Варіант A:** Витягнути в standalone async function (shared між VideoProcessor і WhisperVideoProcessor)
- **Варіант B:** Скопіювати в новий VideoProcessor (простіше, legacy не змінюється)

Рекомендація: **Варіант A** — менше дублювання. Створити `ingestion/_audio.py` з `extract_audio()`.

### 4. Оновити ingestion/factory.py

```python
# БУЛО:
def create_processors(heavy: HeavySteps) -> dict[SourceType, SourceProcessor]:
    return {
        SourceType.VIDEO: VideoProcessor(transcribe_func=heavy.transcribe),
        SourceType.PRESENTATION: PresentationProcessor(...),
        SourceType.TEXT: TextProcessor(),
        SourceType.WEB: WebProcessor(...),
    }

# СТАЄ:
def create_processors(
    heavy: HeavySteps,
    orchestrator: TranscriptionOrchestrator,
) -> dict[SourceType, SourceProcessor]:
    return {
        SourceType.VIDEO: VideoProcessor(orchestrator=orchestrator),
        SourceType.PRESENTATION: PresentationProcessor(...),
        SourceType.TEXT: TextProcessor(),
        SourceType.WEB: WebProcessor(...),
    }
```

### 5. Оновити worker DI (api/tasks.py)

Знайти де створюється `create_processors()` і додати orchestrator:

```python
# В arq_ingest_material або setup:
from course_supporter.stt.factory import create_transcription_orchestrator

orchestrator = create_transcription_orchestrator(settings, service_call_repo)
processors = create_processors(heavy, orchestrator=orchestrator)
```

**ВАЖЛИВО:** Перевірити де саме створюється VideoProcessor — може бути в кількох місцях:
- `api/tasks.py` (arq_ingest_material)
- `worker.py` (startup)
- Тести

### 6. Cleanup TranscribeFunc

Перевірити чи `TranscribeFunc` з `heavy_steps.py` ще використовується де-небудь:
- Якщо тільки VideoProcessor — видалити з `HeavySteps` dataclass
- Якщо інші processors використовують — залишити
- `local_transcribe()` з `transcribe.py` — залишити (може використовуватись WhisperVideoProcessor)

---

## Очікуваний результат

VideoProcessor — thin wrapper навколо STT chain. End-to-end: video upload → yt-dlp → ffmpeg → STT chain → SourceDocument → processed_content

---

## Тестування

### Автоматизовані тести

Файл: `tests/unit/test_stt_factory.py` + `tests/unit/test_video_processor.py`

**Factory tests:**
- chain='deepgram,soniox' + deepgram_api_key only → [DeepgramProvider] (soniox skipped with warning)
- chain='deepgram' + no API key → ValueError
- chain='unknown_provider' → ValueError
- chain='deepgram,elevenlabs' + both keys → [DeepgramProvider, ElevenLabsProvider] in order

**VideoProcessor tests (mock orchestrator):**
- Mock orchestrator returns TranscriptionReport → verify SourceDocument:
  - `source_type == SourceType.VIDEO`
  - `chunks` have `chunk_type == ChunkType.TRANSCRIPT`
  - `chunks[i].metadata` has `start_sec`, `end_sec`, `speaker`
  - `metadata` has `stt_provider`, `stt_model`, `total_cost_usd`, `attempts_count`
- Empty segments → empty chunks (but valid SourceDocument)
- Metadata `strategy` field: `f"stt:{provider}"`

**Integration test:**
- Create orchestrator with mock providers → VideoProcessor.process() with mock source → SourceDocument

### Ручний контроль (Human testing)

1. Upload відео через API: `POST /nodes/{id}/materials` з YouTube URL
2. Дочекатись ingestion (poll job status)
3. Перевірити `processed_content` (GET material): містить транскрипт?
4. Перевірити `ExternalServiceCall` (DB query): запис з `action='transcription'`?
5. Якщо перший provider зафейлив — є запис з `success=false` + другий з `success=true`?

---

## Сумісність з існуючим кодом

- **КРИТИЧНО:** знайти ВСІ місця де створюється `VideoProcessor`:
  - `ingestion/factory.py` → `create_processors()`
  - `api/tasks.py` → `arq_ingest_material`
  - Тести: `tests/unit/test_video*.py`, `tests/integration/test_ingestion*.py`
- `SourceDocument.chunks` format: `ContentChunk(chunk_type=ChunkType.TRANSCRIPT, metadata={"start_sec": float})` — перевірити що MergeStep/downstream не зламається
- `HeavySteps.transcribe` field: перевірити чи ще потрібен (якщо тільки для VideoProcessor — можна прибрати)
- Backward compatibility тестів: mock `TranscribeFunc` → mock `TranscriptionOrchestrator`

---

## Checklist перед PR

- [ ] `stt/factory.py` з `create_stt_providers()` і `create_transcription_orchestrator()`
- [ ] `VideoProcessor` — thin wrapper з `_extract_audio()` + `_to_source_document()`
- [ ] Legacy classes (Gemini/Whisper) **не видалені**, залишаються в video.py
- [ ] `_extract_audio()` refactored (standalone або copied)
- [ ] `ingestion/factory.py` — `create_processors()` приймає orchestrator
- [ ] Worker DI оновлений
- [ ] Код проходить `make check`
- [ ] Unit tests для factory і VideoProcessor
- [ ] Існуючі тести оновлені (mock Orchestrator замість TranscribeFunc)

---

## Нотатки

_Простір для нотаток виконавця:_
- [ ] Де саме створюється VideoProcessor? (api/tasks.py? worker.py?)
- [ ] Чи TranscribeFunc ще потрібен для інших processors?
- [ ] _extract_audio() — refactor в shared function чи copy?
