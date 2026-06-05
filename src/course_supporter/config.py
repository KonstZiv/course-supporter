"""Centralized application configuration via environment variables."""

import zoneinfo
from datetime import time
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, PrivateAttr, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from course_supporter.key_pool import KeyPool


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TESTING = "testing"


_LLM_PROVIDER_KEYS = (
    "gemini",
    "anthropic",
    "openai",
    "deepseek",
    "deepseek_thinking",
    "mistral",
    "dashscope",
)
_STT_PROVIDER_KEYS = ("elevenlabs", "deepgram")
_ALL_PROVIDER_KEYS = _LLM_PROVIDER_KEYS + _STT_PROVIDER_KEYS


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All LLM API keys use SecretStr to prevent accidental logging.
    Keys may contain multiple comma/whitespace-separated values
    for round-robin rotation across provider quotas.

    Database URL is assembled from individual components to match
    the official PostgreSQL Docker image environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
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

    # --- Worker ---
    worker_max_jobs: int = 1
    worker_job_timeout: int = 21600
    worker_max_tries: int = 3
    worker_heavy_window_start: time = time(2, 0)
    worker_heavy_window_end: time = time(6, 30)
    worker_heavy_window_enabled: bool = False
    worker_heavy_window_tz: str = "UTC"
    worker_immediate_override: bool = True

    # --- Webhook delivery ---
    webhook_timeout_seconds: int = 30
    webhook_max_retries: int = 3

    @field_validator("worker_heavy_window_tz")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            zoneinfo.ZoneInfo(v)
        except (zoneinfo.ZoneInfoNotFoundError, KeyError) as err:
            msg = f"Invalid timezone: {v!r}"
            raise ValueError(msg) from err
        return v

    # --- Safety Checker ---
    safety_archive_max_uncompressed_mb: int = 50
    safety_archive_max_files: int = 1000
    safety_archive_max_nesting: int = 1
    safety_max_content_chars: int = 100_000
    homework_max_content_chars: int = 80_000

    # --- S3 / MinIO ---
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: SecretStr = SecretStr("minioadmin")
    s3_bucket: str = "course-materials"

    # --- LLM API Keys ---
    # Raw fields hold the original env value (may be comma-separated).
    # Access via @property (e.g. settings.gemini_api_key) for a single
    # rotated key, or via key_pool_for("gemini") for the full pool.
    gemini_api_key_raw: SecretStr | None = Field(
        None, validation_alias="gemini_api_key"
    )
    anthropic_api_key_raw: SecretStr | None = Field(
        None, validation_alias="anthropic_api_key"
    )
    openai_api_key_raw: SecretStr | None = Field(
        None, validation_alias="openai_api_key"
    )
    deepseek_api_key_raw: SecretStr | None = Field(
        None, validation_alias="deepseek_api_key"
    )
    # Same ENV var (DEEPSEEK_API_KEY) powers both DeepSeek providers — the
    # thinking-on sibling lives in its own key pool so factory.create_providers
    # instantiates a distinct provider class while sharing the operator's
    # single DeepSeek quota. Precedent: alibaba_api_key drives both the
    # DashScope-native pool and (if added) any sibling. (KD-2.4-T wiring.)
    deepseek_thinking_api_key_raw: SecretStr | None = Field(
        None, validation_alias="deepseek_api_key"
    )
    mistral_api_key_raw: SecretStr | None = Field(
        None, validation_alias="mistral_api_key"
    )
    # Internal field name uses provider naming (dashscope) while the
    # env var keeps the operator-facing brand name (ALIBABA_API_KEY).
    dashscope_api_key_raw: SecretStr | None = Field(
        None, validation_alias="alibaba_api_key"
    )
    elevenlabs_api_key_raw: SecretStr | None = Field(
        None, validation_alias="elevenlabs_api_key"
    )
    deepgram_api_key_raw: SecretStr | None = Field(
        None, validation_alias="deepgram_api_key"
    )

    _key_pools: dict[str, KeyPool] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        for name in _ALL_PROVIDER_KEYS:
            secret: SecretStr | None = getattr(self, f"{name}_api_key_raw")
            if secret is not None:
                self._key_pools[name] = KeyPool(secret.get_secret_value())

    # --- Backward-compatible @property accessors ---
    # Each call returns the next key from the round-robin pool.

    @property
    def gemini_api_key(self) -> SecretStr | None:
        pool = self._key_pools.get("gemini")
        return pool.next_key() if pool else None

    @property
    def anthropic_api_key(self) -> SecretStr | None:
        pool = self._key_pools.get("anthropic")
        return pool.next_key() if pool else None

    @property
    def openai_api_key(self) -> SecretStr | None:
        pool = self._key_pools.get("openai")
        return pool.next_key() if pool else None

    @property
    def deepseek_api_key(self) -> SecretStr | None:
        pool = self._key_pools.get("deepseek")
        return pool.next_key() if pool else None

    @property
    def deepseek_thinking_api_key(self) -> SecretStr | None:
        pool = self._key_pools.get("deepseek_thinking")
        return pool.next_key() if pool else None

    @property
    def mistral_api_key(self) -> SecretStr | None:
        pool = self._key_pools.get("mistral")
        return pool.next_key() if pool else None

    @property
    def dashscope_api_key(self) -> SecretStr | None:
        pool = self._key_pools.get("dashscope")
        return pool.next_key() if pool else None

    @property
    def elevenlabs_api_key(self) -> SecretStr | None:
        pool = self._key_pools.get("elevenlabs")
        return pool.next_key() if pool else None

    @property
    def deepgram_api_key(self) -> SecretStr | None:
        pool = self._key_pools.get("deepgram")
        return pool.next_key() if pool else None

    def key_pool_for(self, provider: str) -> KeyPool | None:
        """Return the full key pool for a provider, or None."""
        return self._key_pools.get(provider)

    # --- LLM Default Models ---
    # Configurable per environment via env vars.
    # Defaults can be overridden via external_services.yaml registry.
    gemini_default_model: str = "gemini-2.5-flash"
    anthropic_default_model: str = "claude-sonnet-4-20250514"
    openai_default_model: str = "gpt-4o-mini"
    deepseek_default_model: str = "deepseek-chat"
    # Thinking-on sibling defaults to V4 Pro (reasoning-tier flagship; the only
    # current consumer is video Pass 2a rung 1 per KD-2.4-T).
    deepseek_thinking_default_model: str = "deepseek-v4-pro"
    mistral_default_model: str = "mistral-large-2512"
    dashscope_default_model: str = "qwen3-vl-32b-instruct"

    # --- STT Default Models ---
    elevenlabs_default_model: str = "scribe_v1"
    openai_stt_default_model: str = "gpt-4o-mini-transcribe"
    deepgram_default_model: str = "nova-3"

    # --- DeepSeek ---
    # DeepSeek uses OpenAI-compatible API via OpenAI SDK with custom base_url.
    # Other providers have their own SDKs with built-in endpoints.
    deepseek_base_url: str = "https://api.deepseek.com"
    # Same endpoint as deepseek_base_url — the thinking-on sibling only differs
    # in the omitted thinking-disable hook (provider-side), not the endpoint.
    deepseek_thinking_base_url: str = "https://api.deepseek.com"

    # --- Mistral ---
    # Mistral uses OpenAI-compatible API via OpenAI SDK with custom base_url.
    mistral_base_url: str = "https://api.mistral.ai/v1"

    # --- Alibaba DashScope ---
    # SDK uses module-level globals for base URL; DashScopeProvider sets
    # `dashscope.base_http_api_url` at init time. Default points to the
    # Singapore International endpoint; operators using MaaS workspaces
    # (eu-central-1, etc.) override DASHSCOPE_BASE_URL in their env.
    dashscope_base_url: str = "https://dashscope-intl.aliyuncs.com/api/v1"

    # --- Registries ---
    external_services_path: Path = Path("config/external_services.yaml")
    auth_registry_path: Path = Path("config/auth.yaml")
    platform_registry_path: Path = Path("config/platforms.yaml")
    language_registry_path: Path = Path("config/languages.yaml")
    ladders_dir: Path = Path("config")

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
