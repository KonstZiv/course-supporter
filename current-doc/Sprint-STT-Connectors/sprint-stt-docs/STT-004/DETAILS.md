# STT-004: Soniox connector — Деталі для виконавця

**Sprint:** STT Connectors
**Оцінка:** 4-6h (SDK research included)

---

## Мета

Працюючий конектор до Soniox з code-switching support (uk↔ru↔en)

## Контекст

Ця задача є частиною Sprint STT (6-8 днів).

**Soniox specifics:**
- Найкращий для code-switching (mid-sentence switch uk↔ru↔en)
- Per-token language detection — кожне слово має detected language
- Найдешевший (~$0.0017/min, ~$0.10/hr)
- SDK може бути нестабільним або відсутнім — готуватись до REST API через httpx

## Залежності

**Попередня задача:** [STT-002: STTSettings](../STT-002/README.md)
**Паралельні задачі:** [STT-003: Deepgram](../STT-003/README.md), [STT-005: ElevenLabs](../STT-005/README.md)
**Наступна задача:** [STT-006: TranscriptionOrchestrator](../STT-006/README.md)

---

## Детальний план реалізації

### 0. Research (перший крок!)

**Обов'язково перед написанням коду:**

1. Перевірити PyPI: `pip index versions soniox` / `soniox-api` / `soniox-client`
2. Перевірити Soniox API docs: https://docs.soniox.com (або актуальний URL)
3. З'ясувати:
   - Чи є Python SDK? Яка версія? Async support?
   - REST API endpoints: URL, auth header format, request/response schema
   - Audio delivery: binary upload, URL, або file path?
   - Async processing: submit → poll → result? Або sync?
   - Response format: segments, words, per-token language?
4. **Задокументувати findings в нотатках** (внизу цього файлу)

### 1. Залежності

В `pyproject.toml` `[stt]` extra:
```toml
# Якщо SDK існує:
"soniox>=1.0",
# Якщо SDK не існує — httpx вже є в deps, нічого додавати
```

### 2. `src/course_supporter/stt/providers/soniox.py`

**Варіант A: з SDK**
```python
class SonioxProvider(STTProvider):
    """Soniox transcription with code-switching support."""

    @property
    def name(self) -> str:
        return "soniox"

    def __init__(self, *, api_key: str, model: str = "soniox-v3") -> None:
        self._api_key = api_key
        self._model = model
        # self._client = SonioxClient(api_key=api_key)  # якщо SDK

    def has_valid_config(self) -> bool:
        return bool(self._api_key)

    async def transcribe(self, audio: AudioInput, *, language="uk", ...) -> TranscriptResult:
        additional = additional_languages or []
        hints = [language] + additional  # ['uk', 'ru', 'en']

        # SDK call or REST API — залежить від research
        result = await self._transcribe_impl(audio, hints, diarize)
        return self._to_result(result)
```

**Варіант B: REST API через httpx**
```python
import httpx

class SonioxProvider(STTProvider):
    """Soniox transcription via REST API."""

    BASE_URL = "https://api.soniox.com/v1"  # перевірити!

    async def transcribe(self, audio: AudioInput, ...) -> TranscriptResult:
        async with httpx.AsyncClient(timeout=600) as client:
            # Submit job
            audio_bytes = audio.best_path.read_bytes()
            response = await client.post(
                f"{self.BASE_URL}/transcribe",
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"audio": ("audio.wav", audio_bytes, audio.content_type)},
                data={
                    "model": self._model,
                    "language_hints": ",".join(hints),
                    "enable_diarization": "true",
                },
            )
            self._check_response(response)

            # Якщо async — poll for result
            job_id = response.json().get("job_id")
            if job_id:
                result = await self._poll_result(client, job_id)
            else:
                result = response.json()

        return self._to_result(result)

    async def _poll_result(self, client: httpx.AsyncClient, job_id: str) -> dict:
        """Poll for async job result with timeout."""
        import asyncio
        max_attempts = 120  # 10 min at 5s intervals
        for _ in range(max_attempts):
            resp = await client.get(
                f"{self.BASE_URL}/transcripts/{job_id}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            data = resp.json()
            if data.get("status") == "completed":
                return data
            if data.get("status") == "failed":
                raise STTServerError(self.name, f"Job {job_id} failed: {data}")
            await asyncio.sleep(5)
        raise STTTimeoutError(self.name, f"Job {job_id} polling timeout")
```

### 3. Response mapping

Soniox per-token language — ключова відмінність:
```python
def _to_result(self, response: dict) -> TranscriptResult:
    # Group tokens into segments
    # Each token may have: text, start, end, speaker, language
    segments = self._tokens_to_segments(response.get("tokens", []))

    # Detect primary and all languages
    all_langs = {seg.language for seg in segments if seg.language}

    return TranscriptResult(
        text=response.get("text", ""),
        segments=segments,
        language_detected=max(all_langs, key=lambda l: sum(1 for s in segments if s.language == l)) if all_langs else None,
        languages_detected=sorted(all_langs),
        duration_seconds=response.get("duration", 0.0),
        provider=self.name,
        model=self._model,
        raw_response=response,
    )
```

### 4. Error handling

```python
def _check_response(self, response: httpx.Response) -> None:
    if response.status_code == 401 or response.status_code == 403:
        raise STTAuthError(self.name, f"Auth failed: {response.text}")
    if response.status_code == 429:
        raise STTRateLimitError(self.name, f"Rate limited: {response.text}")
    if response.status_code >= 500:
        raise STTServerError(self.name, f"Server error {response.status_code}: {response.text}")
    response.raise_for_status()  # catch other errors
```

---

## Очікуваний результат

SonioxProvider транскрибує з per-token language detection і code-switching

---

## Тестування

### Автоматизовані тести

Файл: `tests/unit/test_stt_soniox.py`

- Unit test з mock response: `_to_result()` маппінг
- Unit test: `language_hints` формуються правильно — language='uk', additional=['ru', 'en'] → hints=['uk', 'ru', 'en']
- Unit test: per-token language → `segment.language` заповнений
- Unit test: `languages_detected` — всі унікальні мови з segments
- Unit test: `_estimate_cost()` — 120 sec → ~$0.0034
- Unit test: error mapping — 401, 429, 500
- Unit test: async polling — mock success після 2 polls
- Unit test: async polling — mock timeout → STTTimeoutError
- Unit test: `has_valid_config()` — empty → False, "sx_xxx" → True

### Ручний контроль (Human testing)

Запустити на тому ж 10-хв аудіо що і Deepgram. Порівняти:
1. Code-switching quality — чи правильно визначає де uk, де ru, де en?
2. Суржик handling — чи не плутає мови при змішуванні?
3. Загальна якість тексту vs Deepgram
4. `languages_detected` — адекватний список?

---

## Сумісність з існуючим кодом

- Якщо SDK не існує: httpx вже є в залежностях проєкту (перевірити `pyproject.toml`)
- Якщо httpx немає — додати, він легкий і стандартний
- Async polling: `asyncio.sleep(5)` між polls, max timeout 10 min — сумісно з ARQ worker
- `AudioInput.best_path` — binary upload або file path
- WAV 16kHz mono: перевірити що Soniox приймає (деякі API мають sample rate limits)

---

## Checklist перед PR

- [ ] Research задокументований в нотатках (SDK vs REST, audio delivery, response format)
- [ ] `SonioxProvider` реалізує `STTProvider` ABC
- [ ] Code-switching: language_hints передаються правильно
- [ ] Per-token language → segment.language
- [ ] Async polling (якщо потрібен) з timeout
- [ ] Error mapping на STT exception hierarchy
- [ ] Код проходить `make check`
- [ ] Unit tests з mock response

---

## Нотатки

_Простір для нотаток виконавця — обов'язково задокументувати:_
- [ ] SDK exists? Name? Version? Async support?
- [ ] REST API base URL and endpoints?
- [ ] Audio delivery mode (binary/URL/file)?
- [ ] Sync or async processing?
- [ ] Response format (tokens, words, segments)?
