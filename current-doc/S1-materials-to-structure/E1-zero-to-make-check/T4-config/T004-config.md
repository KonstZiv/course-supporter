# 📋 S1-004: Конфігурація додатку

## Мета

Реалізувати централізовану конфігурацію додатку через Pydantic Settings: типізований доступ до змінних середовища, валідація при старті, складання DATABASE_URL з окремих компонентів. Після виконання — додаток при старті валідує всі налаштування і падає з зрозумілим повідомленням, якщо чогось не вистачає.

## Контекст

Залежить від S1-001 (`.env.example`, pydantic-settings у залежностях) та S1-003 (Docker Compose визначає реальні значення змінних). Ця задача замінює заглушку `config.py` з S1-001 на повноцінну реалізацію.

---

## Acceptance Criteria

- [x] `from course_supporter.config import settings` працює
- [x] `settings.database_url` повертає зібраний psycopg URL
- [x] При відсутності обов'язкової змінної — `ValidationError` з описом що саме пропущено
- [x] API keys мають тип `SecretStr` — не логуються і не серіалізуються у plaintext
- [x] Конфігурація завантажується з `.env` файлу автоматично
- [x] `settings.is_dev` / `settings.is_prod` — зручні property для умовної логіки

---

## src/course_supporter/config.py

```python
"""Centralized application configuration via environment variables."""

from functools import lru_cache
from enum import StrEnum

from pydantic import SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TESTING = "testing"


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All LLM API keys use SecretStr to prevent accidental logging.
    Database URL is assembled from individual components to match
    the official PostgreSQL Docker image environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ігноруємо змінні, яких немає в моделі
    )

    # --- App ---
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "DEBUG"

    # --- PostgreSQL ---
    # Змінні збігаються з docker image pgvector/pgvector:pg17
    postgres_user: str = "course_supporter"
    postgres_password: SecretStr = SecretStr("secret")
    postgres_db: str = "course_supporter"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Assemble database URL from components.

        Uses psycopg v3 driver which supports both sync (create_engine)
        and async (create_async_engine) modes natively.
        """
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # --- S3 / MinIO ---
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: SecretStr = SecretStr("minioadmin")
    s3_bucket: str = "course-materials"

    # --- LLM API Keys ---
    # Усі SecretStr — не потрапляють в логи, repr, serialization
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None

    # --- DeepSeek ---
    deepseek_base_url: str = "https://api.deepseek.com"

    # --- Convenience properties ---
    @property
    def is_dev(self) -> bool:
        return self.environment == Environment.DEVELOPMENT

    @property
    def is_prod(self) -> bool:
        return self.environment == Environment.PRODUCTION

    @property
    def is_testing(self) -> bool:
        return self.environment == Environment.TESTING


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton.

    Usage:
        from course_supporter.config import get_settings
        settings = get_settings()

    Or for dependency injection in FastAPI:
        @app.get("/")
        def root(settings: Settings = Depends(get_settings)):
            ...
    """
    return Settings()


# Зручний alias для прямого імпорту
# from course_supporter.config import settings
settings = get_settings()
```

### Пояснення рішень

**SecretStr для API keys** — `pydantic.SecretStr` при `repr()`, `str()`, `json()` показує `'**********'` замість реального значення. Захист від випадкового логування. Для отримання значення — `.get_secret_value()`.

**LLM keys як Optional** — не всі ключі потрібні одночасно. ModelRouter (S1-009) перевірить наявність ключа перед використанням провайдера. Для MVP достатньо одного ключа — Gemini для ingestion.

**computed_field для database_url** — URL збирається з окремих компонентів, які збігаються зі змінними офіційного PostgreSQL Docker image. Один URL з драйвером `psycopg` (v3) — підтримує і sync (`create_engine`), і async (`create_async_engine`) режими нативно.

**lru_cache singleton** — Settings створюється один раз, повторні виклики `get_settings()` повертають кешований об'єкт. Підтримує і прямий імпорт (`from config import settings`), і DI через FastAPI `Depends`.

**extra="ignore"** — `.env` може містити змінні, яких немає в Settings (наприклад, Docker-специфічні). Без цього Pydantic кидає помилку.

---

## Тести

### tests/unit/test_config.py

```python
"""Tests for application configuration."""

import pytest
from pydantic import ValidationError

from course_supporter.config import Environment, Settings


class TestSettings:
    """Test Settings model validation and computed fields."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Settings loads with all defaults (no env vars needed)."""
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        s = Settings(
            _env_file=None,  # не читати .env в тестах
        )
        assert s.environment == Environment.DEVELOPMENT
        assert s.postgres_user == "course_supporter"
        assert s.is_dev is True
        assert s.is_prod is False

    def test_database_url_assembly(self) -> None:
        """Database URL is correctly assembled from components."""
        s = Settings(
            postgres_user="user",
            postgres_password="pass",  # type: ignore[arg-type]
            postgres_host="db.example.com",
            postgres_port=5433,
            postgres_db="mydb",
            _env_file=None,
        )
        assert s.database_url == (
            "postgresql+psycopg://user:pass@db.example.com:5433/mydb"
        )

    def test_secret_str_not_exposed(self) -> None:
        """API keys are not exposed in repr or string conversion."""
        s = Settings(
            gemini_api_key="super-secret-key",  # type: ignore[arg-type]
            _env_file=None,
        )
        repr_str = repr(s)
        assert "super-secret-key" not in repr_str
        assert s.gemini_api_key is not None
        assert s.gemini_api_key.get_secret_value() == "super-secret-key"

    def test_api_keys_optional(self) -> None:
        """All API keys are optional by default."""
        s = Settings(_env_file=None)
        assert s.gemini_api_key is None
        assert s.anthropic_api_key is None
        assert s.openai_api_key is None
        assert s.deepseek_api_key is None

    def test_environment_enum(self) -> None:
        """Environment accepts valid values."""
        s = Settings(environment="production", _env_file=None)  # type: ignore[arg-type]
        assert s.is_prod is True
        assert s.is_dev is False

    def test_invalid_environment(self) -> None:
        """Invalid environment value raises ValidationError."""
        with pytest.raises(ValidationError):
            Settings(environment="invalid", _env_file=None)  # type: ignore[arg-type]

    def test_invalid_port(self) -> None:
        """Non-integer port raises ValidationError."""
        with pytest.raises(ValidationError):
            Settings(postgres_port="not-a-number", _env_file=None)  # type: ignore[arg-type]

    def test_deepseek_base_url_default(self) -> None:
        """DeepSeek base URL has correct default."""
        s = Settings(_env_file=None)
        assert s.deepseek_base_url == "https://api.deepseek.com"

    def test_testing_environment(self) -> None:
        """Testing environment flag works."""
        s = Settings(environment="testing", _env_file=None)  # type: ignore[arg-type]
        assert s.is_testing is True
        assert s.is_dev is False
```

---

## Інтеграція з іншими компонентами

### FastAPI (S1-023)

```python
from fastapi import Depends, FastAPI
from course_supporter.config import Settings, get_settings

app = FastAPI()

@app.get("/health")
async def health(settings: Settings = Depends(get_settings)):
    return {"status": "ok", "environment": settings.environment}
```

### Alembic (S1-005)

```python
# alembic/env.py
from course_supporter.config import settings

config.set_main_option("sqlalchemy.url", settings.database_url)
```

### ModelRouter (S1-009)

```python
from course_supporter.config import settings

# Перевірка наявності ключа перед ініціалізацією провайдера
if settings.gemini_api_key:
    providers["gemini"] = GeminiProvider(
        api_key=settings.gemini_api_key.get_secret_value()
    )
if settings.deepseek_api_key:
    providers["deepseek"] = OpenAIProvider(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
    )
```

---

## Кроки виконання

1. Замінити заглушку `src/course_supporter/config.py` на повну реалізацію
2. Створити `tests/unit/test_config.py`
3. `uv run pytest tests/unit/test_config.py` — всі тести зелені
4. `uv run mypy src/course_supporter/config.py` — strict mode OK
5. Перевірити інтеграцію: `uv run python -c "from course_supporter.config import settings; print(settings.database_url)"`
6. Commit + push

---

## Примітки

- **Не додавати `.env` в Git.** Тільки `.env.example`. `.gitignore` з S1-001 вже виключає `.env`.
- **`_env_file=None` в тестах** — ключовий прийом. Без цього тести читатимуть `.env` з файлової системи, що робить їх нестабільними.
- **psycopg (v3) як єдиний DB-драйвер** — замість asyncpg + psycopg2, використовуємо один `psycopg[binary]>=3.2` (в основних залежностях). Підтримує sync і async нативно. `asyncpg` видалено з залежностей.
- Якщо в майбутньому з'являться settings для конкретних агентів (temperature, max_tokens defaults) — вони додаються як nested models через `model_config = SettingsConfigDict(env_nested_delimiter="__")`.
