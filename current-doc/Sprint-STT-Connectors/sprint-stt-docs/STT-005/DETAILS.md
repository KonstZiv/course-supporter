# STT-005: ElevenLabs connector — Деталі для виконавця

**Sprint:** STT Connectors
**Оцінка:** 4h

---

## Мета

Працюючий конектор до ElevenLabs Scribe v2 з keyterm prompting

## Контекст

Ця задача є частиною Sprint STT (6-8 днів).

**ElevenLabs specifics:**
- Scribe v2 — найновіша STT модель
- Keyterm prompting — до 100 terms для покращення розпізнавання технічних слів
- Entity detection — розпізнавання named entities
- До 48 спікерів в diarization
- Audio events tagging: laughter, applause, music
- **ISO 639-3** language codes (ukr, не uk!) — потрібен маппінг
- SDK може бути sync-only — потрібен `asyncio.to_thread()` wrapper

## Залежності

**Попередня задача:** [STT-002: STTSettings](../STT-002/README.md)
**Паралельні задачі:** [STT-003: Deepgram](../STT-003/README.md), [STT-004: Soniox](../STT-004/README.md)
**Наступна задача:** [STT-006: TranscriptionOrchestrator](../STT-006/README.md)

---

## Детальний план реалізації

### 1. Залежності

В `pyproject.toml` `[stt]` extra:
```toml
"elevenlabs>=2.0",
```

### 2. `src/course_supporter/stt/providers/elevenlabs.py`

```python
import asyncio
import structlog
from elevenlabs import ElevenLabs  # перевірити import path

from course_supporter.stt.base import STTProvider
from course_supporter.stt.exceptions import (
    STTAuthError, STTRateLimitError, STTServerError, STTTimeoutError,
)
from course_supporter.stt.models import AudioInput, TranscriptResult, TranscriptSegment

logger = structlog.get_logger()

# ISO 639-1 → ISO 639-3 mapping (ElevenLabs uses 639-3)
LANG_MAP: dict[str, str] = {
    "uk": "ukr",
    "ru": "rus",
    "en": "eng",
    "de": "deu",
    "fr": "fra",
    "es": "spa",
    "pl": "pol",
    # Extend as needed
}

class ElevenLabsProvider(STTProvider):
    """ElevenLabs Scribe v2 transcription with keyterm prompting."""

    @property
    def name(self) -> str:
        return "elevenlabs"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "scribe_v2",
        diarize: bool = True,
        tag_audio_events: bool = False,
        keyterms: list[str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._diarize = diarize
        self._tag_audio_events = tag_audio_events
        self._keyterms = keyterms or []
        self._client = ElevenLabs(api_key=api_key)

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
        effective_keyterms = (keyterms or self._keyterms)[:100]  # max 100 terms
        lang_code = self._map_language(language)

        # File upload — read bytes from best path (compressed if available)
        audio_bytes = audio.best_path.read_bytes()

        try:
            # SDK may be sync-only → run in thread
            result = await asyncio.to_thread(
                self._client.speech_to_text.convert,
                file=audio_bytes,
                model_id=self._model,
                language_code=lang_code,
                diarize=diarize and self._diarize,
                tag_audio_events=self._tag_audio_events,
                additional_formats=[],  # check SDK
            )
            # Pass keyterms if SDK supports it
            # result = await asyncio.to_thread(
            #     self._client.speech_to_text.convert,
            #     ...,
            #     keyterms=effective_keyterms,
            # )
        except Exception as exc:
            raise self._map_error(exc) from exc

        return self._to_result(result)

    async def health_check(self) -> bool:
        try:
            models = await asyncio.to_thread(self._client.models.get_all)
            return any(m.model_id == self._model for m in models)
        except Exception:
            return False

    @staticmethod
    def _map_language(iso639_1: str) -> str:
        """Map ISO 639-1 → ISO 639-3 for ElevenLabs API."""
        mapped = LANG_MAP.get(iso639_1)
        if mapped is None:
            logger.warning("elevenlabs_unknown_language", language=iso639_1, fallback="eng")
            return "eng"
        return mapped

    def _to_result(self, response) -> TranscriptResult:
        """Map ElevenLabs response → TranscriptResult."""
        # Response structure depends on SDK version — adapt based on actual response
        # Expected: response.text, response.words (list with start, end, text, speaker)

        text = getattr(response, "text", "") or ""
        words = getattr(response, "words", []) or []

        segments = self._words_to_segments(words)

        # Language detection from response
        lang = getattr(response, "language_code", None)
        detected = self._reverse_map_language(lang) if lang else None

        duration = getattr(response, "duration", 0.0) or 0.0

        return TranscriptResult(
            text=text,
            segments=segments,
            language_detected=detected,
            languages_detected=[detected] if detected else [],
            duration_seconds=duration,
            provider=self.name,
            model=self._model,
            raw_response=response.dict() if hasattr(response, "dict") else None,
        )

    def _words_to_segments(self, words) -> list[TranscriptSegment]:
        """Group words by speaker changes or pauses > 2 seconds."""
        if not words:
            return []

        segments: list[TranscriptSegment] = []
        current_words = [words[0]]
        PAUSE_THRESHOLD = 2.0

        for word in words[1:]:
            prev = current_words[-1]
            speaker_changed = getattr(word, "speaker", None) != getattr(prev, "speaker", None)
            pause = getattr(word, "start", 0) - getattr(prev, "end", 0)

            if speaker_changed or pause > PAUSE_THRESHOLD:
                segments.append(self._finalize_segment(current_words))
                current_words = [word]
            else:
                current_words.append(word)

        if current_words:
            segments.append(self._finalize_segment(current_words))

        return segments

    def _finalize_segment(self, words) -> TranscriptSegment:
        text = " ".join(getattr(w, "text", str(w)) for w in words)
        speaker = getattr(words[0], "speaker", None)
        confidence = None  # ElevenLabs may not provide per-word confidence

        return TranscriptSegment(
            text=text,
            start=getattr(words[0], "start", 0.0),
            end=getattr(words[-1], "end", 0.0),
            speaker=f"speaker_{speaker}" if speaker is not None else None,
            confidence=confidence,
            language=None,
        )

    @staticmethod
    def _reverse_map_language(iso639_3: str) -> str | None:
        """Map ISO 639-3 back to 639-1."""
        reverse = {v: k for k, v in LANG_MAP.items()}
        return reverse.get(iso639_3)

    def estimate_cost(self, duration_seconds: float) -> float | None:
        """Estimate cost. ~$0.0067/min."""
        return (duration_seconds / 60) * 0.0067

    def _map_error(self, exc: Exception) -> Exception:
        msg = str(exc)
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

### 3. ISO 639-3 mapping details

ElevenLabs **вимагає** ISO 639-3 language codes. Решта системи використовує ISO 639-1.

| ISO 639-1 | ISO 639-3 | Language |
|---|---|---|
| uk | ukr | Ukrainian |
| ru | rus | Russian |
| en | eng | English |

Маппінг робиться **в provider**, не в Orchestrator — бо це ElevenLabs-specific. Інші providers використовують 639-1 напряму.

### 4. Keyterms handling

- Max 100 terms — truncate якщо більше (з warning в log)
- Terms передаються для покращення розпізнавання технічних слів
- Перевірити SDK: чи параметр називається `keyterms`, `keywords`, `additional_vocab`?
- Якщо SDK не підтримує — перевірити REST API

### 5. Sync SDK → async wrapper

ElevenLabs Python SDK (`elevenlabs` package) може бути **sync-only**. Рішення:

```python
result = await asyncio.to_thread(
    self._client.speech_to_text.convert,
    file=audio_bytes,
    ...
)
```

`asyncio.to_thread()` запускає sync функцію в thread executor, не блокуючи event loop. Перевірити що SDK не має async variant (`AsyncElevenLabs`?).

---

## Очікуваний результат

ElevenLabsProvider транскрибує з keyterm prompting, ISO 639-3 mapping працює прозоро

---

## Тестування

### Автоматизовані тести

Файл: `tests/unit/test_stt_elevenlabs.py`

- Unit test з mock: `_to_result()` маппінг
- Unit test `_map_language()`: 'uk' → 'ukr', 'ru' → 'rus', 'en' → 'eng', unknown → 'eng' з warning
- Unit test `_reverse_map_language()`: 'ukr' → 'uk', 'rus' → 'ru'
- Unit test: keyterms truncation — 150 terms → 100
- Unit test: `_words_to_segments()` — group by speaker, group by pause
- Unit test: diarization speaker labels
- Unit test: `_estimate_cost()` — 120 sec → ~$0.0134
- Unit test: `_map_error()` — 401, 429, 500, timeout
- Unit test: `has_valid_config()` — empty → False, "el_xxx" → True

### Ручний контроль (Human testing)

10-хв аудіо з keyterms=['Python', 'Django', 'ORM', 'PostgreSQL', 'міграція', 'ендпоінт']:
1. Чи keyterms покращили розпізнавання? (порівняти з run без keyterms)
2. Якість порівняно з Deepgram і Soniox?
3. Entity detection в raw_response?

---

## Сумісність з існуючим кодом

- `elevenlabs` SDK v2+: перевірити що `speech_to_text` module існує
- ISO 639-3 → 639-1: reverse mapping в `_reverse_map_language()` для `language_detected`
- `AudioInput.best_path`: file upload preferred, S3 не потрібен
- WAV 16kHz mono: перевірити upload, можливо потрібна MP3 конвертація (Orchestrator робить це автоматично для великих файлів)
- Sync SDK: `asyncio.to_thread()` не блокує event loop

---

## Checklist перед PR

- [ ] `ElevenLabsProvider` реалізує `STTProvider` ABC
- [ ] ISO 639-3 mapping працює (uk→ukr, ru→rus, en→eng)
- [ ] Keyterms передаються, max 100 з truncation
- [ ] Sync SDK wrapped в `asyncio.to_thread()`
- [ ] Words → segments grouping
- [ ] Error mapping на STT exception hierarchy
- [ ] Код проходить `make check`
- [ ] Unit tests з mock response

---

## Нотатки

_Простір для нотаток виконавця:_
- [ ] SDK async variant exists? (`AsyncElevenLabs`?)
- [ ] Keyterms parameter name in SDK?
- [ ] Response structure (text, words, duration)?
- [ ] File upload size limit?
