# STT-002: STTSettings + конфігурація provider chain — Деталі для виконавця

**Sprint:** STT Connectors
**Оцінка:** 2h

---

## Мета

Вся конфігурація STT через env variables з валідацією

## Контекст

Ця задача є частиною Sprint STT (6-8 днів).

**Загальна ціль:** підключити три зовнішні STT сервіси з конфігурованим fallback chain. STT chain повністю замінює Gemini Vision + Whisper як default video transcription pipeline.

## Залежності

**Попередня задача:** [STT-001: STT Provider ABC + уніфіковані моделі](../STT-001/README.md)
**Наступна задача:** [STT-003: Deepgram connector](../STT-003/README.md)

---

## Детальний план реалізації

### 1. Створити `src/course_supporter/stt/settings.py`

```python
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

KNOWN_PROVIDERS = {"deepgram", "soniox", "elevenlabs"}

class STTSettings(BaseSettings):
    """STT provider chain configuration."""

    # -- Provider priority (ordered, comma-separated) --
    stt_provider_chain: str = "deepgram,soniox,elevenlabs"

    # -- Language hints --
    stt_default_language: str = "uk"            # ISO 639-1
    stt_language_detection: bool = False
    stt_additional_languages: str = ""           # "ru,en" — comma-separated

    # -- Deepgram --
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    deepgram_tier: str = "batch"                # batch | streaming
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
    stt_compress_threshold_mb: int = 50        # WAV→MP3 if file > this size (MB)

    model_config = SettingsConfigDict(env_prefix="", env_file=".env")

    # -- Parsed properties --

    @property
    def provider_chain(self) -> list[str]:
        """Parse comma-separated chain into ordered list."""
        return [name.strip() for name in self.stt_provider_chain.split(",") if name.strip()]

    @property
    def additional_languages_list(self) -> list[str]:
        """Parse comma-separated languages."""
        return [lang.strip() for lang in self.stt_additional_languages.split(",") if lang.strip()]

    @property
    def fallback_error_types(self) -> set[str]:
        """Parse comma-separated fallback error types."""
        return {e.strip() for e in self.stt_fallback_on_errors.split(",") if e.strip()}

    # -- Validators --

    @field_validator("stt_provider_chain")
    @classmethod
    def validate_provider_chain(cls, v: str) -> str:
        chain = [name.strip() for name in v.split(",") if name.strip()]
        if not chain:
            raise ValueError("STT provider chain cannot be empty")
        for name in chain:
            if name not in KNOWN_PROVIDERS:
                raise ValueError(f"Unknown STT provider: {name}. Known: {KNOWN_PROVIDERS}")
        return v

    @field_validator("stt_fallback_on_errors")
    @classmethod
    def validate_fallback_errors(cls, v: str) -> str:
        valid = {"timeout", "5xx", "rate_limit", "auth", "quota", "all"}
        errors = {e.strip() for e in v.split(",") if e.strip()}
        unknown = errors - valid
        if unknown:
            raise ValueError(f"Unknown fallback error types: {unknown}. Valid: {valid}")
        return v
```

### 2. Інтегрувати в `Settings` (config.py)

Перевірити поточну структуру `Settings` в `config.py`. Два варіанти:

**Варіант A (nested, preferred):**
```python
class Settings(BaseSettings):
    # ... existing fields ...
    stt: STTSettings = STTSettings()
```

**Варіант B (якщо Settings flat):**
Flatten всі STT поля в основний Settings. Менш бажано, але може бути потрібно якщо Settings не підтримує nested.

### 3. Оновити `.env.example`

Додати блок з коментарями:
```bash
# ═══════════════════════════════════════════════════════
# STT Provider Chain
# ═══════════════════════════════════════════════════════
# Ordered list of STT providers. First working provider wins.
# Fallback: if primary fails (timeout/5xx) → next in chain.
STT_PROVIDER_CHAIN=deepgram,soniox,elevenlabs
STT_DEFAULT_LANGUAGE=uk
STT_ADDITIONAL_LANGUAGES=ru,en

# -- Deepgram (https://deepgram.com) --
# Nova-3 batch mode. ~$0.0043/min.
DEEPGRAM_API_KEY=
DEEPGRAM_MODEL=nova-3
DEEPGRAM_KEYTERMS=Python,Django,FastAPI,PostgreSQL

# -- Soniox (https://soniox.com) --
# Best for code-switching (uk↔ru↔en). ~$0.0017/min.
SONIOX_API_KEY=
SONIOX_MODEL=soniox-v3

# -- ElevenLabs (https://elevenlabs.io) --
# Scribe v2 with keyterm prompting. ~$0.0067/min.
ELEVENLABS_API_KEY=
ELEVENLABS_MODEL=scribe_v2
ELEVENLABS_KEYTERMS=Python,Django,ORM,PostgreSQL

# -- Fallback policy --
STT_MAX_RETRIES_PER_PROVIDER=2
STT_PROVIDER_TIMEOUT=600
STT_FALLBACK_ON_ERRORS=timeout,5xx,rate_limit

# -- Audio preprocessing --
# Compress WAV→MP3 before sending to STT if file > threshold (MB)
STT_COMPRESS_THRESHOLD_MB=50
```

### 4. Допоміжні функції

```python
def parse_csv_keyterms(raw: str) -> list[str]:
    """Parse comma-separated keyterms. Empty string → empty list."""
    return [term.strip() for term in raw.split(",") if term.strip()]
```

Ці функції використовуються в factory (STT-007) при створенні providers.

---

## Очікуваний результат

`settings.stt.provider_chain` повертає `['deepgram', 'soniox', 'elevenlabs']`
`settings.stt.fallback_error_types` повертає `{'timeout', '5xx', 'rate_limit'}`
`.env.example` оновлений з усіма STT змінними

---

## Тестування

### Автоматизовані тести

Файл: `tests/unit/test_stt_settings.py`

- Unit test: default values — chain='deepgram,soniox,elevenlabs', language='uk', compress=100
- Unit test: `provider_chain` property — 'soniox' → ['soniox'], 'deepgram,elevenlabs' → ['deepgram', 'elevenlabs']
- Unit test: `additional_languages_list` — 'ru,en' → ['ru', 'en'], '' → []
- Unit test: `fallback_error_types` — 'timeout,5xx' → {'timeout', '5xx'}
- Unit test: `parse_csv_keyterms()` — 'Python,Django,FastAPI' → ['Python', 'Django', 'FastAPI']
- Unit test: validator — unknown provider 'whisper' → ValueError
- Unit test: validator — empty chain '' → ValueError
- Unit test: validator — unknown fallback error 'foo' → ValueError
- Unit test: env override — `STT_PROVIDER_CHAIN=soniox` → `provider_chain == ['soniox']`
- Unit test: integration with Settings — `settings.stt.deepgram_api_key` accessible

### Ручний контроль (Human testing)

Перевірити `.env.example` — чи зрозуміло новому розробнику: які ключі обов'язкові, як змінити chain, що означає fallback policy?

---

## Сумісність з існуючим кодом

- Перевірити як організований `Settings` в `config.py`:
  - Чи використовує `model_config` з `env_file`?
  - Чи є nested `BaseSettings` (приклад: `minio: MinioSettings`)?
  - Чи flat (всі поля в одному класі)?
- Не ламати існуючі settings — STT поля **додаються**, нічого не видаляється
- `env_prefix=""` — перевірити що STT_ / DEEPGRAM_ / SONIOX_ / ELEVENLABS_ не конфліктують з існуючими змінними
- API keys як `str`, не `SecretStr` (для consistency — перевірити як інші API keys зберігаються в Settings)

---

## Checklist перед PR

- [ ] `STTSettings` з усіма полями і validators
- [ ] Інтегровано в `Settings` (config.py)
- [ ] `.env.example` оновлений з коментарями
- [ ] `parse_csv_keyterms()` helper
- [ ] Код проходить `make check` (ruff + mypy + pytest)
- [ ] Unit tests покривають parsing, defaults, validation, env overrides
- [ ] Існуючі тести не зламані

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
