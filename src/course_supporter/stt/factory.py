"""STT provider factory — creates providers based on available API keys."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from course_supporter.config import Settings
from course_supporter.stt.providers import STT_PROVIDER_REGISTRY, STTProvider

logger = structlog.get_logger()


@dataclass(frozen=True)
class STTProviderFactoryConfig:
    """Typed configuration for creating an STT provider instance."""

    get_default_model: Callable[[Settings], str]
    key_pool_name: str | None = None
    extra_kwargs: dict[str, Any] = field(default_factory=dict)


STT_PROVIDER_CONFIGS: dict[str, STTProviderFactoryConfig] = {
    "elevenlabs": STTProviderFactoryConfig(
        get_default_model=lambda s: s.elevenlabs_default_model,
    ),
    "openai_stt": STTProviderFactoryConfig(
        get_default_model=lambda s: s.openai_stt_default_model,
        key_pool_name="openai",
    ),
    "deepgram": STTProviderFactoryConfig(
        get_default_model=lambda s: s.deepgram_default_model,
    ),
}


def create_stt_providers(settings: Settings) -> dict[str, STTProvider]:
    """Instantiate STT providers for all configured API keys.

    Returns dict: provider_name -> STTProvider instance.
    Only providers with non-None API keys are created.
    """
    providers: dict[str, STTProvider] = {}

    for name, provider_cls in STT_PROVIDER_REGISTRY.items():
        config = STT_PROVIDER_CONFIGS.get(name)
        if config is None:
            continue

        pool_name = config.key_pool_name or name
        pool = settings.key_pool_for(pool_name)
        if pool is None:
            continue

        api_keys = [k.get_secret_value() for k in pool.all_keys()]

        kwargs: dict[str, Any] = {"api_keys": api_keys}
        kwargs["default_model"] = config.get_default_model(settings)
        kwargs.update(config.extra_kwargs)

        providers[name] = provider_cls(**kwargs)
        logger.info("stt_provider_registered", provider=name, key_count=len(api_keys))

    if not providers:
        logger.warning("no_stt_providers_configured")

    return providers
