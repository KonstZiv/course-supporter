"""Provider factory -- creates providers based on available API keys.

Uses PROVIDER_REGISTRY for extensibility. Adding a new provider
requires NO changes to this file.
"""

from typing import Any

import structlog

from course_supporter.config import Settings
from course_supporter.llm.providers import PROVIDER_REGISTRY, LLMProvider

logger = structlog.get_logger()

# Mapping provider_name -> config for creating instances
PROVIDER_CONFIG: dict[str, dict[str, Any]] = {
    "gemini": {
        "key_attr": "gemini_api_key",
    },
    "anthropic": {
        "key_attr": "anthropic_api_key",
    },
    "openai": {
        "key_attr": "openai_api_key",
    },
    "deepseek": {
        "key_attr": "deepseek_api_key",
        "extra_kwargs": {
            "provider_name": "deepseek",
            "default_model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
        },
    },
}


def create_providers(settings: Settings) -> dict[str, LLMProvider]:
    """Instantiate providers for all configured API keys.

    Returns dict: provider_name -> LLMProvider instance.
    Only providers with non-None API keys are created.
    """
    providers: dict[str, LLMProvider] = {}

    for name, provider_cls in PROVIDER_REGISTRY.items():
        config = PROVIDER_CONFIG.get(name, {})
        key_attr = config.get("key_attr")
        if not key_attr:
            continue

        api_key_secret = getattr(settings, key_attr, None)
        if api_key_secret is None:
            continue

        kwargs: dict[str, Any] = {"api_key": api_key_secret.get_secret_value()}
        kwargs.update(config.get("extra_kwargs", {}))

        providers[name] = provider_cls(**kwargs)
        logger.info("llm_provider_registered", provider=name)

    if not providers:
        logger.warning("no_llm_providers_configured")

    return providers
