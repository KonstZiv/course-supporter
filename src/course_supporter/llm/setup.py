"""One-stop factory for assembling the full LLM stack.

Usage::

    from course_supporter.config import get_settings
    from course_supporter.llm import create_model_router

    router = create_model_router(get_settings())
    response = await router.complete("course_structuring", prompt)
"""

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.config import Settings
from course_supporter.llm.factory import create_providers
from course_supporter.llm.logging import create_log_callback
from course_supporter.llm.registry import load_registry
from course_supporter.llm.router import ModelRouter

logger = structlog.get_logger()


def create_model_router(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    max_attempts: int = 2,
) -> ModelRouter:
    """Assemble ModelRouter with providers, registry, and optional DB logging.

    Args:
        settings: Application settings with API keys and registry path.
        session_factory: If provided, LLM calls are logged to llm_calls table.
        max_attempts: Max retry attempts per model (default 2).

    Returns:
        Configured ModelRouter ready for use.
    """
    registry = load_registry(settings.model_registry_path)
    providers = create_providers(settings)

    log_callback = None
    if session_factory is not None:
        log_callback = create_log_callback(session_factory)
        logger.info("llm_db_logging_enabled")

    router = ModelRouter(
        providers=providers,
        registry=registry,
        log_callback=log_callback,
        max_attempts=max_attempts,
    )
    logger.info(
        "model_router_created",
        providers=list(providers.keys()),
        max_attempts=max_attempts,
        db_logging=session_factory is not None,
    )
    return router
