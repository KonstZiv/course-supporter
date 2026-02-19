"""Centralized application configuration via environment variables."""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

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
        extra="ignore",
    )

    # --- App ---
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "DEBUG"
    # --- CORS ---
    cors_allowed_origins: list[str] = []
    cors_allow_credentials: bool = False
    cors_allowed_methods: list[str] = ["GET", "POST"]
    cors_allowed_headers: list[str] = ["Content-Type", "X-API-Key"]

    # --- PostgreSQL ---
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

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- S3 / MinIO ---
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: SecretStr = SecretStr("minioadmin")
    s3_bucket: str = "course-materials"

    # --- LLM API Keys ---
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None

    # --- LLM Default Models ---
    # Configurable per environment via env vars.
    # Will be superseded by models.yaml registry in S1-008.
    gemini_default_model: str = "gemini-2.5-flash"
    anthropic_default_model: str = "claude-sonnet-4-20250514"
    openai_default_model: str = "gpt-4o-mini"
    deepseek_default_model: str = "deepseek-chat"

    # --- DeepSeek ---
    # DeepSeek uses OpenAI-compatible API via OpenAI SDK with custom base_url.
    # Other providers have their own SDKs with built-in endpoints.
    deepseek_base_url: str = "https://api.deepseek.com"

    # --- Model Registry ---
    model_registry_path: Path = Path("config/models.yaml")

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

    Usage::

        from course_supporter.config import get_settings
        settings = get_settings()

    Or for dependency injection in FastAPI::

        @app.get("/")
        def root(settings: Settings = Depends(get_settings)):
            ...
    """
    return Settings()


settings = get_settings()
