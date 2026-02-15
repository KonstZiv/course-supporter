"""Provider factory -- creates providers based on available API keys.

Uses PROVIDER_REGISTRY for extensibility. Adding a new provider
requires only a new entry in PROVIDER_CONFIGS.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import SecretStr

from course_supporter.config import Settings
from course_supporter.llm.providers import PROVIDER_REGISTRY, LLMProvider

logger = structlog.get_logger()


@dataclass(frozen=True)
class ProviderFactoryConfig:
    """Typed configuration for creating an LLM provider instance."""

    get_api_key: Callable[[Settings], SecretStr | None]
    get_default_model: Callable[[Settings], str]
    get_base_url: Callable[[Settings], str] | None = None
    extra_kwargs: dict[str, Any] = field(default_factory=dict)


PROVIDER_CONFIGS: dict[str, ProviderFactoryConfig] = {
    "gemini": ProviderFactoryConfig(
        get_api_key=lambda s: s.gemini_api_key,
        get_default_model=lambda s: s.gemini_default_model,
    ),
    "anthropic": ProviderFactoryConfig(
        get_api_key=lambda s: s.anthropic_api_key,
        get_default_model=lambda s: s.anthropic_default_model,
    ),
    "openai": ProviderFactoryConfig(
        get_api_key=lambda s: s.openai_api_key,
        get_default_model=lambda s: s.openai_default_model,
    ),
    "deepseek": ProviderFactoryConfig(
        get_api_key=lambda s: s.deepseek_api_key,
        get_default_model=lambda s: s.deepseek_default_model,
        get_base_url=lambda s: s.deepseek_base_url,
        extra_kwargs={"provider_name": "deepseek"},
    ),
}


def create_providers(settings: Settings) -> dict[str, LLMProvider]:
    """Instantiate providers for all configured API keys.

    Returns dict: provider_name -> LLMProvider instance.
    Only providers with non-None API keys are created.
    """
    providers: dict[str, LLMProvider] = {}

    for name, provider_cls in PROVIDER_REGISTRY.items():
        config = PROVIDER_CONFIGS.get(name)
        if config is None:
            continue

        api_key_secret = config.get_api_key(settings)
        if api_key_secret is None:
            continue

        kwargs: dict[str, Any] = {"api_key": api_key_secret.get_secret_value()}
        kwargs["default_model"] = config.get_default_model(settings)

        if config.get_base_url is not None:
            kwargs["base_url"] = config.get_base_url(settings)

        kwargs.update(config.extra_kwargs)

        providers[name] = provider_cls(**kwargs)
        logger.info("llm_provider_registered", provider=name)

    if not providers:
        logger.warning("no_llm_providers_configured")

    return providers
