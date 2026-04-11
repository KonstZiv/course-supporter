"""One-stop factory for STTRouter creation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from course_supporter.config import Settings
from course_supporter.llm.registry import load_registry
from course_supporter.stt.factory import create_stt_providers
from course_supporter.stt.router import STTRouter

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger()


def create_stt_router(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    max_attempts: int = 2,
) -> STTRouter:
    """Create a fully wired STTRouter.

    Args:
        settings: Application settings with API keys.
        session_factory: Optional async session factory for DB logging.
        max_attempts: Max retry attempts per provider (transient errors).

    Returns:
        Configured STTRouter with all available providers.
    """
    registry = load_registry(settings.external_services_path)
    providers = create_stt_providers(settings)

    log_callback = None
    if session_factory is not None:
        from course_supporter.service_logging import create_stt_log_callback

        log_callback = create_stt_log_callback(session_factory)
        logger.info("stt_db_logging_enabled")

    logger.info(
        "stt_router_created",
        providers=list(providers.keys()),
        max_attempts=max_attempts,
    )

    return STTRouter(providers, registry, log_callback, max_attempts)
