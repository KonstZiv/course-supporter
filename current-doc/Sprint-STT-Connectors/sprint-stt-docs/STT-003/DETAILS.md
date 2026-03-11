# STT-003: Deepgram connector — Деталі для виконавця

**Sprint:** STT Connectors
**Оцінка:** 4h

---

## Мета

Працюючий конектор до Deepgram Nova-3 batch API

## Контекст

Ця задача є частиною Sprint STT (6-8 днів).

**Загальна ціль:** три STT сервіси як injectable connectors. STT chain повністю замінює Gemini Vision + Whisper.

**Deepgram specifics:**
- Nova-3 — найновіша модель, найкраща якість
- Batch mode — дешевше ($0.0043/min vs $0.0059/min streaming)
- Binary upload — SDK приймає bytes напряму, S3 не потрібен
- Keyterms — boosted terms для покращення розпізнавання технічних слів

## Залежності

**Попередня задача:** [STT-002: STTSettings + конфігурація](../STT-002/README.md)
**Паралельні задачі:** [STT-004: Soniox](../STT-004/README.md), [STT-005: ElevenLabs](../STT-005/README.md)
**Наступна задача:** [STT-006: TranscriptionOrchestrator](../STT-006/README.md)

---

## Детальний план реалізації

### 1. Залежності

В `pyproject.toml` додати в optional deps:
```toml
[project.optional-dependencies]
stt = [
    "deepgram-sdk>=4.0",
    # soniox, elevenlabs — додаються в STT-004, STT-005
]
```

### 2. Структура файлів

```
src/course_supporter/stt/providers/
├── __init__.py       # re-export всіх providers
└── deepgram.py       # DeepgramProvider
```

### 3. `src/course_supporter/stt/providers/deepgram.py`

```python
import structlog
from deepgram import DeepgramClient, PrerecordedOptions

from course_supporter.stt.base import STTProvider
from course_supporter.stt.exceptions import (
    STTAuthError, STTRateLimitError, STTServerError, STTTimeoutError,
)
from course_supporter.stt.models import AudioInput, TranscriptResult, TranscriptSegment

logger = structlog.get_logger()

class DeepgramProvider(STTProvider):
    """Deepgram Nova-3 batch transcription via binary upload."""

    @property
    def name(self) -> str:
        return "deepgram"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "nova-3",
        tier: str = "batch",
        smart_format: bool = True,
        diarize: bool = True,
        punctuate: bool = True,
        keyterms: list[str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._tier = tier
        self._smart_format = smart_format
        self._diarize = diarize
        self._punctuate = punctuate
        self._keyterms = keyterms or []
        self._client = DeepgramClient(api_key)

    def has_valid_config(self) -> bool:
        return bool(self._api_key)

    async def transcribe(
        self,
        audio: AudioInput,
        *,
        language: str = "uk",
        additional_languages: list[str] | None = None,
        keyterms: list[str] | None = None,
        diarize: bool = True,
    ) -> TranscriptResult:
        """Transcribe via binary upload (preferred) or URL fallback."""
        effective_keyterms = keyterms or self._keyterms

        options = PrerecordedOptions(
            model=self._model,
            language=language,
            smart_format=self._smart_format,
            diarize=diarize and self._diarize,
            punctuate=self._punctuate,
            keywords=effective_keyterms,  # boosted terms
        )

        try:
            # Binary upload — preferred, no S3 needed
            audio_bytes = audio.best_path.read_bytes()
            payload = {"buffer": audio_bytes, "mimetype": audio.content_type}
            response = await self._client.listen.asyncrest.v("1").transcribe_file(
                payload, options
            )
        except Exception as exc:
            raise self._map_error(exc) from exc

        return self._to_result(response)

    async def health_check(self) -> bool:
        try:
            # Lightweight: list projects
            await self._client.manage.asyncrest.v("1").get_projects()
            return True
        except Exception:
            return False

    def _to_result(self, response) -> TranscriptResult:
        """Map Deepgram response → TranscriptResult."""
        channel = response.results.channels[0]
        alternative = channel.alternatives[0]

        # Full text
        text = alternative.transcript

        # Group words into segments by speaker changes and pauses
        segments = self._words_to_segments(alternative.words)

        # Duration from metadata
        duration = response.metadata.duration if hasattr(response, "metadata") else 0.0

        return TranscriptResult(
            text=text,
            segments=segments,
            language_detected=response.results.channels[0].detected_language,
            languages_detected=[response.results.channels[0].detected_language] if hasattr(channel, "detected_language") else [],
            duration_seconds=duration,
            provider=self.name,
            model=self._model,
            raw_response=response.to_dict() if hasattr(response, "to_dict") else None,
        )

    def _words_to_segments(self, words) -> list[TranscriptSegment]:
        """Group words by speaker changes or pauses > 2 seconds."""
        if not words:
            return []

        segments: list[TranscriptSegment] = []
        current_words: list = [words[0]]
        PAUSE_THRESHOLD = 2.0  # seconds

        for word in words[1:]:
            prev = current_words[-1]
            speaker_changed = getattr(word, "speaker", None) != getattr(prev, "speaker", None)
            pause_too_long = word.start - prev.end > PAUSE_THRESHOLD

            if speaker_changed or pause_too_long:
                segments.append(self._finalize_segment(current_words))
                current_words = [word]
            else:
                current_words.append(word)

        if current_words:
            segments.append(self._finalize_segment(current_words))

        return segments

    def _finalize_segment(self, words) -> TranscriptSegment:
        text = " ".join(getattr(w, "punctuated_word", w.word) for w in words)
        avg_confidence = sum(w.confidence for w in words) / len(words) if words else None
        speaker = getattr(words[0], "speaker", None)

        return TranscriptSegment(
            text=text,
            start=words[0].start,
            end=words[-1].end,
            speaker=f"speaker_{speaker}" if speaker is not None else None,
            confidence=avg_confidence,
            language=None,  # Deepgram detects per-channel, not per-word
        )

    def estimate_cost(self, duration_seconds: float) -> float | None:
        """Estimate cost. Batch: $0.0043/min."""
        rate = 0.0043 if self._tier == "batch" else 0.0059
        return (duration_seconds / 60) * rate

    def _map_error(self, exc: Exception) -> Exception:
        """Map SDK/HTTP exceptions to STT error hierarchy."""
        msg = str(exc)
        # Deepgram SDK raises different exceptions for different HTTP statuses
        # Adapt based on actual SDK exception types
        if "401" in msg or "403" in msg or "Unauthorized" in msg:
            return STTAuthError(self.name, msg)
        if "429" in msg or "rate" in msg.lower():
            return STTRateLimitError(self.name, msg)
        if "500" in msg or "502" in msg or "503" in msg:
            return STTServerError(self.name, msg)
        if "timeout" in msg.lower():
            return STTTimeoutError(self.name, msg)
        from course_supporter.stt.exceptions import STTError
        return STTError(self.name, msg)
```

### 4. Deepgram response structure (для reference)

```
response.results.channels[0].alternatives[0].transcript → full text
response.results.channels[0].alternatives[0].words → list[Word]
response.results.channels[0].detected_language → "uk"
response.metadata.duration → float (seconds)

Word attributes:
  .word: str           # raw word
  .punctuated_word: str  # word with punctuation
  .start: float        # start time in seconds
  .end: float          # end time in seconds
  .confidence: float   # 0.0-1.0
  .speaker: int | None # speaker index (if diarize=True)
```

---

## Очікуваний результат

`DeepgramProvider` транскрибує аудіо через binary upload, повертає `TranscriptResult` з сегментами, таймкодами і speaker labels

---

## Тестування

### Автоматизовані тести

Файл: `tests/unit/test_stt_deepgram.py`

- Unit test з mock response: створити fake Deepgram response object → перевірити `_to_result()` маппінг
- Unit test `_words_to_segments()`: group by speaker — 3 words speaker_0, 2 words speaker_1 → 2 segments
- Unit test `_words_to_segments()`: group by pause — pause > 2s → new segment
- Unit test `_words_to_segments()`: empty words → empty segments
- Unit test `_finalize_segment()`: punctuated_word preferred over word
- Unit test `_estimate_cost()`: 120 sec, batch → $0.0086; 120 sec, streaming → $0.0118
- Unit test `_map_error()`: "401" → STTAuthError, "429" → STTRateLimitError, "500" → STTServerError, "timeout" → STTTimeoutError
- Unit test `has_valid_config()`: empty key → False, "dg_live_xxx" → True
- Unit test `health_check()`: mock success → True, mock exception → False

### Ручний контроль (Human testing)

Integration test на реальному 10-хв українському аудіо:
1. Текст читабельний?
2. Таймкоди адекватні (spot check 5 місць)?
3. Speaker labels правильні (якщо кілька спікерів)?
4. Keyterms ('Python', 'Django') розпізнаються правильно?

---

## Сумісність з існуючим кодом

- `deepgram-sdk` v4+: перевірити сумісність з Python 3.13+ та asyncio
- `AudioInput.best_path` — повертає `compressed_path` (MP3) якщо Orchestrator компресував, інакше `file_path` (WAV)
- WAV 16kHz mono: Deepgram приймає без конвертації
- Для великих файлів: `read_bytes()` на 100MB MP3 — ок для 2-core VPS. Якщо проблема — fallback на URL mode з S3
- Не конфліктує з існуючими залежностями

---

## Checklist перед PR

- [ ] `DeepgramProvider` реалізує `STTProvider` ABC (transcribe, health_check, has_valid_config)
- [ ] Binary upload працює з `AudioInput.best_path`
- [ ] Words → segments grouping по speaker і pauses
- [ ] Error mapping на STT exception hierarchy
- [ ] Код проходить `make check` (ruff + mypy + pytest)
- [ ] Unit tests з mock response
- [ ] Існуючі тести не зламані

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
