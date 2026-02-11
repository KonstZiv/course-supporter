# S1-004: Конфігурація додатку

## Мета

Централізована типізована конфігурація через Pydantic Settings: валідація при старті, складання DATABASE_URL з окремих компонентів, захист API keys через SecretStr. Додаток при старті або працює з правильними налаштуваннями, або падає з зрозумілим повідомленням.

## Що робимо

1. **Settings клас** (`config.py`): Pydantic BaseSettings з полями для PostgreSQL, MinIO, LLM API keys (Gemini, Anthropic, OpenAI, DeepSeek), app settings
2. **Database URL**: `computed_field` збирає єдиний URL (`postgresql+psycopg://`) з окремих `POSTGRES_*` змінних. psycopg v3 підтримує sync і async нативно.
3. **SecretStr** для всіх API keys та паролів — не потрапляють у логи, repr, serialization
4. **LLM keys як Optional** — не всі потрібні одночасно, ModelRouter перевірить наявність при ініціалізації
5. **Singleton** через `@lru_cache` — підтримує і прямий імпорт, і FastAPI `Depends`
6. **Тести**: 9 unit-тестів на валідацію, defaults, SecretStr, computed fields

## Очікуваний результат

```python
from course_supporter.config import settings
settings.database_url       # → "postgresql+psycopg://user:pass@localhost:5432/db"
settings.gemini_api_key     # → SecretStr('**********') або None
settings.is_dev             # → True
```

## Контрольні точки

- [ ] `from course_supporter.config import settings` — імпорт працює
- [ ] `settings.database_url` — коректний psycopg URL (`postgresql+psycopg://`)
- [ ] `repr(settings)` — API keys показуються як `'**********'`
- [ ] Settings без `.env` файлу — працює з defaults
- [ ] Невалідне значення `ENVIRONMENT=invalid` — `ValidationError`
- [ ] `uv run pytest tests/unit/test_config.py` — 9 тестів зелені

## Залежності

- **Блокується:** S1-001 (`.env.example`, pydantic-settings)
- **Блокує:** S1-005 (Alembic потребує database_url), S1-009 (ModelRouter потребує API keys)

## Деталі

Повний spec (код config.py, тести, інтеграція з FastAPI/Alembic/ModelRouter): **T004-config.md**
