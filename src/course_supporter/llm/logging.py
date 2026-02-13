"""Database logging callback for LLM calls.

Provides create_log_callback() that returns a LogCallback function
compatible with ModelRouter. Each LLM call (success or failure)
is persisted to the llm_calls table in a separate DB session.

DB errors are swallowed and logged -- LLM call flow is never interrupted.
"""

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.llm.router import LogCallback
from course_supporter.llm.schemas import LLMResponse
from course_supporter.storage.orm import LLMCall

logger = structlog.get_logger()


def create_log_callback(
    session_factory: async_sessionmaker[AsyncSession],
) -> LogCallback:
    """Create a LogCallback that persists LLM calls to the database.

    Args:
        session_factory: Async session factory for creating isolated
            DB sessions per log entry.

    Returns:
        Async callback matching ModelRouter's LogCallback signature.
    """

    async def _log_to_db(
        response: LLMResponse,
        success: bool,
        error_message: str | None,
    ) -> None:
        record = LLMCall(
            action=response.action,
            strategy=response.strategy,
            provider=response.provider,
            model_id=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
            success=success,
            error_message=error_message,
        )
        try:
            async with session_factory() as session:
                session.add(record)
                await session.commit()
        except Exception:
            logger.error(
                "llm_call_log_failed",
                provider=response.provider,
                model=response.model_id,
                action=response.action,
                exc_info=True,
            )

    return _log_to_db
