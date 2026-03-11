# STT-001: STT Provider ABC + уніфіковані моделі — Деталі для виконавця

**Sprint:** STT Connectors
**Оцінка:** 3h

---

## Мета

Єдиний контракт для всіх STT провайдерів і уніфіковані моделі результатів

## Контекст

Ця задача є частиною Sprint STT (6-8 днів).

**Загальна ціль:** підключити три зовнішні STT сервіси (Deepgram, Soniox, ElevenLabs) як injectable connectors з конфігурованим fallback chain. STT chain **повністю замінює** Gemini Vision + Whisper як default video transcription pipeline.

**Архітектурний контекст:**
- Sprint 2 AR-5 визначив injectable `TranscribeFunc`. Цей спрінт замінює його на `TranscriptionOrchestrator` з provider chain.
- `GeminiVideoProcessor` / `WhisperVideoProcessor` залишаються в коді як legacy, але не використовуються в default pipeline.
- VideoProcessor стає тонкою обгорткою: extract audio → STT chain → SourceDocument.

## Залежності

**Наступна задача:** [STT-002: STTSettings + конфігурація provider chain](../STT-002/README.md)

---

## Детальний план реалізації

### 1. Створити пакет `src/course_supporter/stt/`

```
src/course_supporter/stt/
├── __init__.py          # re-export ключових типів
├── base.py              # STTProvider ABC
├── models.py            # AudioInput, TranscriptSegment, TranscriptResult, etc.
└── exceptions.py        # STTError hierarchy
```

### 2. `src/course_supporter/stt/base.py`

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

        AudioInput.file_path is always populated (local WAV 16kHz mono from yt-dlp + ffmpeg).
        AudioInput.compressed_path may be populated (MP3, for large files — set by Orchestrator).
        Provider decides how to deliver audio:
        - binary upload: read bytes from compressed_path (if exists) or file_path
        - URL: use audio.s3_url if available, otherwise upload to S3

        Raises:
            STTAuthError: invalid API key (NOT fallback — config error)
            STTRateLimitError: rate limit exceeded (fallback)
            STTTimeoutError: provider timeout (fallback)
            STTServerError: 5xx from provider (fallback)
            STTQuotaError: quota exceeded (fallback)
            STTError: other errors
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check provider availability with lightweight API call."""
        ...

    @abstractmethod
    def has_valid_config(self) -> bool:
        """Check if provider has required configuration (API key etc).

        Used by factory to skip unconfigured providers with warning.
        """
        ...

    def estimate_cost(self, duration_seconds: float) -> float | None:
        """Estimate transcription cost in USD.

        Not abstract — default returns None. Override in concrete providers
        with provider-specific pricing.
        """
        return None
```

### 3. `src/course_supporter/stt/models.py`

**ВАЖЛИВО: Pydantic BaseModel, не dataclass.** Весь проєкт використовує Pydantic для data models.

```python
from pathlib import Path
from pydantic import BaseModel

class AudioInput(BaseModel):
    """Input for STT providers — local audio file with optional alternatives."""
    file_path: Path                    # local WAV (16kHz mono) — always populated
    compressed_path: Path | None = None  # MP3 — set by Orchestrator for large files
    s3_url: str | None = None          # presigned S3 URL — for URL-only providers
    content_type: str = "audio/wav"

    @property
    def best_path(self) -> Path:
        """Return compressed path if available, otherwise original.

        Note: all providers get the same file (MP3 if compressed, WAV otherwise).
        If a provider works better with a specific format, it can ignore best_path
        and use file_path directly.
        """
        return self.compressed_path or self.file_path

class TranscriptSegment(BaseModel):
    """Single transcript segment with timestamps."""
    text: str
    start: float              # seconds from beginning
    end: float                # seconds from beginning
    speaker: str | None = None       # "speaker_0", "speaker_1", ...
    confidence: float | None = None  # 0.0-1.0
    language: str | None = None      # detected language (ISO 639-1)

class TranscriptResult(BaseModel):
    """Unified result from any STT provider."""
    text: str                                    # full text
    segments: list[TranscriptSegment]            # segments with timestamps
    language_detected: str | None = None         # primary language
    languages_detected: list[str] = []           # all detected languages
    duration_seconds: float                      # audio duration
    provider: str                                # "deepgram" | "soniox" | "elevenlabs"
    model: str                                   # "nova-3" | "soniox-v3" | "scribe_v2"
    raw_response: dict[str, Any] | None = None   # original response (debug)

class TranscriptionAttempt(BaseModel):
    """Single transcription attempt (for Orchestrator reporting)."""
    provider: str
    model: str
    success: bool
    duration_ms: int
    error: str | None = None
    error_type: str | None = None     # "auth", "timeout", "5xx", "rate_limit", "quota"
    cost_usd: float | None = None
    result: TranscriptResult | None = None  # only if success=True

class TranscriptionReport(BaseModel):
    """Full report from Orchestrator with all attempts."""
    result: TranscriptResult                    # successful result
    attempts: list[TranscriptionAttempt]        # all attempts (incl. failed)
    total_duration_ms: int                      # total time of all attempts
    total_cost_usd: float                       # total cost of all attempts
```

**Зв'язок з існуючими моделями:**
- В `heavy_steps.py` вже є `Transcript` і `TranscriptSegment` — вони залишаються для backward compatibility
- Маппінг `TranscriptResult` → `SourceDocument.chunks` буде в STT-007 (VideoProcessor)
- `TranscriptionAttempt` — lightweight in-memory DTO для Orchestrator; персистентний audit trail — через `ExternalServiceCall` ORM

### 4. `src/course_supporter/stt/exceptions.py`

```python
class STTError(Exception):
    """Base STT error."""
    error_type: str = "unknown"

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")

class STTAuthError(STTError):
    """401/403 — config error. NOT retryable, NOT fallback."""
    error_type = "auth"

class STTRateLimitError(STTError):
    """429 — rate limit. Retryable, fallback."""
    error_type = "rate_limit"

class STTTimeoutError(STTError):
    """Provider timeout. Fallback."""
    error_type = "timeout"

class STTServerError(STTError):
    """5xx from provider. Fallback."""
    error_type = "5xx"

class STTQuotaError(STTError):
    """Quota exceeded. Fallback."""
    error_type = "quota"
```

`error_type` як class variable — дозволяє Orchestrator робити `except STTAuthError` і одночасно `attempt.error_type = exc.error_type` для fallback decision.

### 5. `src/course_supporter/stt/__init__.py`

Re-export ключових типів для зручності:
```python
from course_supporter.stt.base import STTProvider
from course_supporter.stt.exceptions import (STTError, STTAuthError, ...)
from course_supporter.stt.models import (AudioInput, TranscriptResult, ...)
```

---

## Очікуваний результат

Пакет `stt/` з чистими абстракціями, готовий до реалізації конекторів в STT-003/004/005

---

## Тестування

### Автоматизовані тести

Файл: `tests/unit/test_stt_models.py`

- Unit test `TranscriptSegment`: створення, boundary values (start=0, end=0, confidence=0.0/1.0)
- Unit test `TranscriptResult`: створення з segments, серіалізація `.model_dump_json()`
- Unit test `TranscriptionAttempt`: success=True з result, success=False з error
- Unit test `TranscriptionReport`: `total_cost_usd` правильно сумує кілька attempts
- Unit test `AudioInput`: `file_path` обов'язковий, `best_path` повертає `compressed_path` якщо є
- Unit test exceptions: `error_type` class variable для кожного типу, `str(exc)` містить provider name
- Unit test exceptions: `isinstance(STTAuthError(...), STTError)` — ієрархія працює
- Type check: `mypy --strict` на `src/course_supporter/stt/`

### Ручний контроль (Human testing)

Code review — чи контракт `STTProvider` достатньо generic? Чи можна додати новий provider (Google Cloud STT, Azure Speech) без зміни ABC? Чи `TranscriptResult` покриває всі потреби VideoProcessor?

---

## Сумісність з існуючим кодом

- В `heavy_steps.py` є `Transcript(BaseModel)` з `segments: list[TranscriptSegment]` і `language: str | None` — **не змінюємо** існуючі моделі. Новий `stt.models.TranscriptSegment` — окремий тип з більшою кількістю полів.
- `ExternalServiceCall` ORM має поля `action`, `strategy`, `provider`, `model_id`, `latency_ms`, `cost_usd`, `success`, `error_message` — `TranscriptionAttempt` маппиться на ці поля без конфліктів.
- Новий пакет `stt/` додається поруч з `ingestion/`, не зачіпає існуючий код.

---

## Checklist перед PR

- [ ] Всі моделі — Pydantic `BaseModel` (не `dataclass`)
- [ ] `STTProvider` ABC має `transcribe()`, `health_check()`, `has_valid_config()`
- [ ] `AudioInput` має `best_path` property для зручності providers
- [ ] Error hierarchy з `error_type` class variable
- [ ] `__init__.py` re-exports ключові типи
- [ ] Код проходить `make check` (ruff + mypy + pytest)
- [ ] Unit tests покривають happy path і edge cases
- [ ] Існуючі тести не зламані

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
