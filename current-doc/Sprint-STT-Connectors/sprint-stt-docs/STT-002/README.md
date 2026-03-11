# STT-002: STTSettings + конфігурація provider chain

**Sprint:** STT Connectors
**Оцінка:** 2h

---

## Мета

Вся конфігурація STT через env variables з валідацією

## Що робимо

Додати `STTSettings` в існуючий `Settings`, оновити `.env.example`

## Як робимо (коротко)

Створити `src/course_supporter/stt/settings.py`:
  - `class STTSettings(BaseSettings)` з усіма полями
  - `stt_provider_chain: str` — comma-separated ordered list
  - Per-provider settings: `deepgram_*`, `soniox_*`, `elevenlabs_*`
  - Fallback policy: `stt_max_retries_per_provider`, `stt_provider_timeout`, `stt_fallback_on_errors`
  - Audio preprocessing: `stt_compress_threshold_mb` (WAV→MP3 для великих файлів)
  - Validators: `parse_provider_chain()`, `parse_csv_keyterms()`, `parse_fallback_errors()`
Інтегрувати в існуючий `Settings` (config.py):
  - Додати `stt: STTSettings` як nested field
Оновити `.env.example` з усіма STT змінними і коментарями

## Очікуваний результат

`settings.stt.stt_provider_chain` повертає `'deepgram,soniox,elevenlabs'`, всі provider-specific settings доступні

## Тестування

**Автоматизовано:**
- Unit test: default values (chain='deepgram,soniox,elevenlabs', language='uk')
- Unit test: parse chain — 'soniox' → ['soniox'], 'deepgram,elevenlabs' → ['deepgram', 'elevenlabs']
- Unit test: parse keyterms — 'Python,Django,FastAPI' → ['Python', 'Django', 'FastAPI']
- Unit test: parse fallback errors — 'timeout,5xx' → {'timeout', '5xx'}
- Unit test: unknown provider in chain → ValueError
- Unit test: empty chain → ValueError
- Unit test: stt_compress_threshold_mb default = 50
- Unit test: env override — STT_PROVIDER_CHAIN=soniox → ['soniox']

**Human control:**
Перевірити `.env.example` — чи зрозуміло новому розробнику що налаштовувати?

## Сумісність з існуючим кодом

- Перевірити як організований `Settings` в `config.py` — nested `BaseSettings` чи flat
- Не ламати існуючі settings — STT поля додаються
- `env_prefix`: перевірити що не конфліктує з існуючими змінними

## Checklist

- [ ] Код написаний і проходить `make check`
- [ ] Unit tests написані і зелені
- [ ] `.env.example` оновлений і зрозумілий
- [ ] Сумісність з існуючим кодом перевірена
- [ ] Human control пройдений
