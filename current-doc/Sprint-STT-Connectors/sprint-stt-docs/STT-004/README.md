# STT-004: Soniox connector

**Sprint:** STT Connectors
**Оцінка:** 4-6h (SDK research included)

---

## Мета

Працюючий конектор до Soniox з code-switching support (uk↔ru↔en)

## Що робимо

Реалізувати `SonioxProvider(STTProvider)`. SDK або REST API через httpx (з'ясувати першим кроком).

## Як робимо (коротко)

**Крок 0 (research):** перевірити PyPI — чи є `soniox` / `soniox-api` / `soniox-client` пакет. Якщо SDK нестабільний або відсутній — REST API через httpx.

Додати залежність в `[stt]` extra
Створити `src/course_supporter/stt/providers/soniox.py`:
  - `class SonioxProvider(STTProvider)`
  - `__init__(api_key, model)`
  - `has_valid_config() → bool`
  - `transcribe(audio: AudioInput, ...)`:
    1. Binary upload або file path (залежить від SDK/API)
    2. `language_hints=['uk', 'ru', 'en']` — code-switching
    3. `enable_diarization=True`
    4. Async polling якщо API async (submit job → poll → result)
    5. Per-token language → зберегти в `segment.language`
    6. Error mapping
  - `health_check()`: lightweight API call
  - `_estimate_cost(duration_seconds)`: ~$0.0017/min

## Очікуваний результат

SonioxProvider транскрибує з автоматичним code-switching uk↔ru↔en

## Тестування

**Автоматизовано:**
- Unit test з mock: Soniox response → TranscriptResult
- Unit test: language_hints передаються
- Unit test: per-token language → segment.language
- Unit test: cost estimation, error mapping
- Unit test: async polling (якщо async API)

**Human control:**
10-хв аудіо з суржиком: порівняти з Deepgram — якість code-switching, detected languages

## Сумісність з існуючим кодом

- SDK може бути нестабільним — httpx fallback (вже є в deps)
- `AudioInput.best_path` для binary upload
- Async polling: `asyncio.sleep` + timeout, сумісно з ARQ worker

## Checklist

- [ ] Код написаний і проходить `make check`
- [ ] Unit tests написані і зелені
- [ ] SDK/REST decision задокументована в нотатках
- [ ] Human control пройдений
