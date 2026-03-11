# STT-006: TranscriptionOrchestrator — fallback chain — Деталі для виконавця

**Sprint:** STT Connectors
**Оцінка:** 4h

---

## Мета

Orchestrator що пробує провайдерів по черзі з retry, fallback і audio preprocessing

## Контекст

Ця задача є частиною Sprint STT (6-8 днів).

**Orchestrator — центральний компонент** STT layer. Він:
1. Приймає `AudioInput` (local WAV з existing pipeline)
2. Компресує великі файли WAV→MP3 (один раз)
3. Пробує провайдерів по черзі згідно `STT_PROVIDER_CHAIN`
4. Retry per provider, fallback на наступного при recoverable errors
5. Логує кожну спробу в `ExternalServiceCall`
6. Повертає `TranscriptionReport` з successful result і всіма attempts

## Залежності

**Попередня задача:** STT-003/004/005 (конектори)
**Наступна задача:** [STT-007: Factory + VideoProcessor інтеграція](../STT-007/README.md)

---

## Детальний план реалізації

### 1. `src/course_supporter/stt/orchestrator.py`

```python
import asyncio
import time
from pathlib import Path

import structlog

from course_supporter.stt.base import STTProvider
from course_supporter.stt.exceptions import STTAuthError, STTError, STTTimeoutError
from course_supporter.stt.models import (
    AudioInput, TranscriptionAttempt, TranscriptionReport, TranscriptResult,
)
from course_supporter.stt.settings import STTSettings

logger = structlog.get_logger()


class TranscriptionOrchestrator:
    """Try STT providers in order with retry, fallback, and audio preprocessing."""

    def __init__(
        self,
        providers: list[STTProvider],
        settings: STTSettings,
        service_call_repo,  # ExternalServiceCallRepository
    ) -> None:
        self._providers = providers
        self._settings = settings
        self._repo = service_call_repo
        self._fallback_errors = settings.fallback_error_types

    async def transcribe(self, audio: AudioInput) -> TranscriptionReport:
        """Transcribe audio using provider chain with fallback.

        Handles audio preprocessing (WAV→MP3 compression) and cleanup.
        """
        # Step 1: Compress large files once (before trying any provider)
        audio = await self._maybe_compress(audio)

        try:
            return await self._run_chain(audio)
        finally:
            # Cleanup: remove compressed MP3 if we created it
            if audio.compressed_path and audio.compressed_path.exists():
                audio.compressed_path.unlink(missing_ok=True)

    async def _run_chain(self, audio: AudioInput) -> TranscriptionReport:
        """Core chain logic — separated for try/finally cleanup in transcribe()."""
        attempts: list[TranscriptionAttempt] = []
        language = self._settings.stt_default_language
        additional = self._settings.additional_languages_list

        for provider in self._providers:
            for retry in range(self._settings.stt_max_retries_per_provider):
                logger.info(
                    "stt_attempt",
                    provider=provider.name,
                    retry=retry,
                    total_attempts=len(attempts),
                )

                attempt = await self._try_provider(
                    provider, audio, language=language, additional_languages=additional,
                )
                attempts.append(attempt)
                await self._log_attempt(attempt)

                if attempt.success and attempt.result is not None:
                    logger.info(
                        "stt_success",
                        provider=provider.name,
                        duration_ms=attempt.duration_ms,
                        cost_usd=attempt.cost_usd,
                    )
                    return TranscriptionReport(
                        result=attempt.result,
                        attempts=attempts,
                        total_duration_ms=sum(a.duration_ms for a in attempts),
                        total_cost_usd=sum(a.cost_usd or 0.0 for a in attempts),
                    )

                if not self._should_fallback(attempt.error_type):
                    # Auth error — config broken, don't try other providers
                    logger.error(
                        "stt_auth_error_no_fallback",
                        provider=provider.name,
                        error=attempt.error,
                    )
                    raise STTAuthError(provider.name, attempt.error or "Config error")

                logger.warning(
                    "stt_attempt_failed",
                    provider=provider.name,
                    error_type=attempt.error_type,
                    error=attempt.error,
                    retry=retry,
                )

            # All retries for this provider exhausted
            logger.warning("stt_provider_exhausted", provider=provider.name)

        # All providers failed
        raise STTError(
            "all",
            f"All {len(self._providers)} providers failed after {len(attempts)} attempts",
        )

    async def _try_provider(
        self,
        provider: STTProvider,
        audio: AudioInput,
        *,
        language: str,
        additional_languages: list[str],
    ) -> TranscriptionAttempt:
        """Single attempt with one provider, with timeout."""
        start = time.monotonic()
        try:
            result: TranscriptResult = await asyncio.wait_for(
                provider.transcribe(
                    audio,
                    language=language,
                    additional_languages=additional_languages,
                    # NOTE: keyterms=None here is intentional. Each provider gets
                    # keyterms from its own __init__ config (via env: DEEPGRAM_KEYTERMS,
                    # ELEVENLABS_KEYTERMS). Orchestrator does NOT manage keyterms —
                    # they are per-provider because each provider has different
                    # capabilities (ElevenLabs: 100 terms, Deepgram: boosted keywords).
                    keyterms=None,
                    diarize=True,
                ),
                timeout=self._settings.stt_provider_timeout,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return TranscriptionAttempt(
                provider=provider.name,
                model=result.model,
                success=True,
                duration_ms=elapsed_ms,
                cost_usd=self._estimate_cost(provider, result.duration_seconds),
                result=result,
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return TranscriptionAttempt(
                provider=provider.name,
                model="unknown",
                success=False,
                duration_ms=elapsed_ms,
                error=f"Timeout after {self._settings.stt_provider_timeout}s",
                error_type="timeout",
            )
        except STTError as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return TranscriptionAttempt(
                provider=provider.name,
                model="unknown",
                success=False,
                duration_ms=elapsed_ms,
                error=str(exc),
                error_type=exc.error_type,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.exception("stt_unexpected_error", provider=provider.name)
            return TranscriptionAttempt(
                provider=provider.name,
                model="unknown",
                success=False,
                duration_ms=elapsed_ms,
                error=str(exc),
                error_type="unknown",
            )

    def _should_fallback(self, error_type: str | None) -> bool:
        """Decide if error should trigger fallback to next provider."""
        if error_type == "auth":
            return False  # NEVER fallback on auth — config is broken
        if "all" in self._fallback_errors:
            return True
        return error_type in self._fallback_errors if error_type else False

    async def _log_attempt(self, attempt: TranscriptionAttempt) -> None:
        """Persist attempt to ExternalServiceCall for audit trail."""
        try:
            await self._repo.create(
                action="transcription",
                strategy=f"stt:{attempt.provider}",
                provider=attempt.provider,
                model_id=attempt.model,
                latency_ms=attempt.duration_ms,
                cost_usd=attempt.cost_usd,
                success=attempt.success,
                error_message=attempt.error,
            )
        except Exception:
            logger.exception("stt_log_attempt_failed", provider=attempt.provider)

    async def _maybe_compress(self, audio: AudioInput) -> AudioInput:
        """Compress WAV→MP3 if file exceeds size threshold.

        This is a one-time operation before the first provider attempt.
        Reduces 700MB WAV (2hr) → ~100MB MP3.
        """
        threshold_bytes = self._settings.stt_compress_threshold_mb * 1024 * 1024
        file_size = audio.file_path.stat().st_size

        if file_size <= threshold_bytes:
            return audio

        mp3_path = audio.file_path.with_suffix(".mp3")
        logger.info(
            "stt_compressing_audio",
            original_mb=file_size / (1024 * 1024),
            threshold_mb=self._settings.stt_compress_threshold_mb,
            output=str(mp3_path),
        )

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", str(audio.file_path),
            "-acodec", "libmp3lame", "-q:a", "4",
            "-y", str(mp3_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("stt_compress_failed", stderr=stderr.decode())
            return audio  # fallback to original WAV

        compressed_size = mp3_path.stat().st_size
        logger.info(
            "stt_compressed",
            original_mb=file_size / (1024 * 1024),
            compressed_mb=compressed_size / (1024 * 1024),
        )

        return audio.model_copy(update={"compressed_path": mp3_path, "content_type": "audio/mpeg"})

    def _estimate_cost(self, provider: STTProvider, duration_seconds: float) -> float | None:
        """Estimate cost via provider's estimate_cost() method."""
        return provider.estimate_cost(duration_seconds)

    async def health_check_all(self) -> dict[str, bool]:
        """Check all providers, return {provider_name: is_healthy}."""
        results: dict[str, bool] = {}
        for provider in self._providers:
            try:
                results[provider.name] = await provider.health_check()
            except Exception:
                results[provider.name] = False
        return results
```

### 2. Audio preprocessing flow

```
AudioInput(file_path=/tmp/audio.wav)  # 700MB, 2hr lecture
    │
    ├── _maybe_compress() checks: 700MB > 100MB threshold
    │   └── ffmpeg -i audio.wav -acodec libmp3lame -q:a 4 audio.mp3
    │
    └── AudioInput(file_path=/tmp/audio.wav, compressed_path=/tmp/audio.mp3)
            │
            ├── Provider calls audio.best_path → /tmp/audio.mp3 (100MB)
            └── All providers use same compressed file
```

Key: compression happens **once**, before the first provider. All providers benefit.

### 3. Fallback decision matrix

| Error type | `stt_fallback_on_errors` | Fallback? |
|---|---|---|
| `auth` | any | **NEVER** (config broken) |
| `timeout` | contains "timeout" or "all" | Yes |
| `5xx` | contains "5xx" or "all" | Yes |
| `rate_limit` | contains "rate_limit" or "all" | Yes |
| `quota` | contains "quota" or "all" | Yes |
| `unknown` | contains "all" | Yes |
| `unknown` | does not contain "all" | No (raise) |

---

## Очікуваний результат

Orchestrator автоматично компресує великі файли, обробляє failures, і переходить до наступного provider з повним audit trail

---

## Тестування

### Автоматизовані тести

Файл: `tests/unit/test_stt_orchestrator.py`

Використовувати **mock providers** (fake `STTProvider` subclass) для unit tests:

```python
class MockProvider(STTProvider):
    def __init__(self, name: str, *, fail_with: Exception | None = None):
        self._name = name
        self._fail_with = fail_with

    @property
    def name(self) -> str: return self._name
    def has_valid_config(self) -> bool: return True
    async def health_check(self) -> bool: return True
    async def transcribe(self, audio, **kwargs) -> TranscriptResult:
        if self._fail_with:
            raise self._fail_with
        return TranscriptResult(text="test", segments=[], ...)
```

Tests:
- **Happy path:** first provider succeeds → 1 attempt, correct result
- **First fails (timeout):** MockProvider(fail_with=STTTimeoutError) → second succeeds → 2 attempts
- **Retry within provider:** first try STTServerError → second try succeeds → 2 attempts same provider
- **Auth error:** STTAuthError → NOT fallback → raise STTAuthError
- **All fail:** 2 providers × 2 retries = 4 attempts → raise STTError('all')
- **Retry count:** max_retries=2 → exactly 2 attempts per provider
- **Fallback policy 'timeout':** timeout fallbacks, 5xx does NOT → raise
- **Fallback policy 'all':** everything fallbacks (except auth)
- **_log_attempt:** mock repo → verify `create()` called with correct args
- **_maybe_compress:** file > threshold → MP3 created (mock ffmpeg or use tiny WAV)
- **_maybe_compress:** file < threshold → audio unchanged
- **health_check_all:** one healthy + one down → correct dict

### Ручний контроль (Human testing)

1. Set wrong API key for first provider
2. Run transcription
3. Check ExternalServiceCall: first attempt with error, second with success
4. Check that fallback was transparent to caller

---

## Сумісність з існуючим кодом

- `ExternalServiceCall` ORM: перевірити що repo `create()` method приймає ці kwargs (action, strategy, provider, model_id, latency_ms, cost_usd, success, error_message)
- Якщо repo має інший API — адаптувати `_log_attempt()`
- `asyncio.wait_for` timeout: перевірити що не конфліктує з ARQ `job_timeout` setting
- ffmpeg: вже встановлений (використовується для audio extraction в existing pipeline)
- `audio.model_copy()`: Pydantic v2 method для immutable copy з updates

---

## Checklist перед PR

- [ ] `TranscriptionOrchestrator` з повною fallback chain logic
- [ ] Audio preprocessing `_maybe_compress()` з ffmpeg
- [ ] Retry per provider, configurable via settings
- [ ] Auth error → NEVER fallback
- [ ] `_log_attempt()` → ExternalServiceCall
- [ ] structlog logging на кожному кроці
- [ ] Код проходить `make check`
- [ ] Unit tests з mock providers (10+ test cases)
- [ ] Існуючі тести не зламані

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
