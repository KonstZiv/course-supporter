"""Provider factory -- creates providers based on available API keys.

Uses PROVIDER_REGISTRY for extensibility. Adding a new provider
requires only a new entry in PROVIDER_CONFIGS.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from course_supporter.config import Settings
from course_supporter.llm.providers import PROVIDER_REGISTRY, LLMProvider

logger = structlog.get_logger()


@dataclass(frozen=True)
class ProviderFactoryConfig:
    """Typed configuration for creating an LLM provider instance."""

    get_default_model: Callable[[Settings], str]
    get_base_url: Callable[[Settings], str] | None = None
    extra_kwargs: dict[str, Any] = field(default_factory=dict)


PROVIDER_CONFIGS: dict[str, ProviderFactoryConfig] = {
    "gemini": ProviderFactoryConfig(
        get_default_model=lambda s: s.gemini_default_model,
    ),
    "anthropic": ProviderFactoryConfig(
        get_default_model=lambda s: s.anthropic_default_model,
    ),
    "openai": ProviderFactoryConfig(
        get_default_model=lambda s: s.openai_default_model,
    ),
    "deepseek": ProviderFactoryConfig(
        get_default_model=lambda s: s.deepseek_default_model,
        get_base_url=lambda s: s.deepseek_base_url,
        extra_kwargs={"provider_name": "deepseek"},
    ),
    "deepseek_thinking": ProviderFactoryConfig(
        get_default_model=lambda s: s.deepseek_thinking_default_model,
        get_base_url=lambda s: s.deepseek_thinking_base_url,
        extra_kwargs={"provider_name": "deepseek_thinking"},
    ),
    "mistral": ProviderFactoryConfig(
        get_default_model=lambda s: s.mistral_default_model,
        get_base_url=lambda s: s.mistral_base_url,
        extra_kwargs={"provider_name": "mistral"},
    ),
    "dashscope": ProviderFactoryConfig(
        get_default_model=lambda s: s.dashscope_default_model,
        get_base_url=lambda s: s.dashscope_base_url,
    ),
}


def create_providers(settings: Settings) -> dict[str, LLMProvider]:
    """Instantiate providers for all configured API keys.

    Returns dict: provider_name -> LLMProvider instance.
    Only providers with non-None API keys are created.
    Each provider receives all keys from the pool for round-robin rotation.
    """
    providers: dict[str, LLMProvider] = {}

    for name, provider_cls in PROVIDER_REGISTRY.items():
        config = PROVIDER_CONFIGS.get(name)
        if config is None:
            continue

        pool = settings.key_pool_for(name)
        if pool is None:
            continue

        api_keys = [k.get_secret_value() for k in pool.all_keys()]

        kwargs: dict[str, Any] = {"api_keys": api_keys}
        kwargs["default_model"] = config.get_default_model(settings)

        if config.get_base_url is not None:
            kwargs["base_url"] = config.get_base_url(settings)

        kwargs.update(config.extra_kwargs)

        providers[name] = provider_cls(**kwargs)
        logger.info("llm_provider_registered", provider=name, key_count=len(api_keys))

    if not providers:
        logger.warning("no_llm_providers_configured")

    return providers
