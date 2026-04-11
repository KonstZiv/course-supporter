"""Unified logging for all external service calls (LLM, STT, VD).

Uses a ``ContextVar`` to propagate ``tenant_id`` from the ARQ task level
down to every external call, regardless of the service type.

Usage in task functions::

    from course_supporter.service_logging import tenant_scope

    async def arq_ingest_material(ctx, ...):
        with tenant_scope(tenant_id):
            ...  # all LLM/STT/VD calls within this scope get tenant_id

Factory functions create typed callbacks compatible with
ModelRouter, STTRouter, and VD pipeline components.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.llm.schemas import LLMResponse
    from course_supporter.stt.schemas import STTResult

logger = structlog.get_logger()

# ── Context variable for per-task tenant isolation ──

_current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar(
    "current_tenant_id", default=None
)


@contextmanager
def tenant_scope(tenant_id: uuid.UUID) -> Iterator[None]:
    """Set tenant_id for the duration of an ARQ task."""
    token = _current_tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _current_tenant_id.reset(token)


def get_current_tenant_id() -> uuid.UUID | None:
    """Read the current tenant_id from context."""
    return _current_tenant_id.get()


# ── Persist to DB ──


async def _persist(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID | None,
    action: str,
    strategy: str,
    provider: str,
    model_id: str,
    unit_type: str | None = None,
    unit_in: int | None = None,
    unit_out: int | None = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    success: bool = True,
    error_message: str | None = None,
) -> None:
    """Write a single ExternalServiceCall row.

    DB errors are swallowed — call flow is never interrupted.
    """
    from course_supporter.storage.orm import ExternalServiceCall

    record = ExternalServiceCall(
        tenant_id=tenant_id or get_current_tenant_id(),
        action=action,
        strategy=strategy,
        provider=provider,
        model_id=model_id,
        unit_type=unit_type,
        unit_in=unit_in,
        unit_out=unit_out,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        success=success,
        error_message=error_message,
    )
    try:
        async with session_factory() as session:
            session.add(record)
            await session.commit()
    except (SQLAlchemyError, OSError):
        logger.error(
            "service_call_log_failed",
            provider=provider,
            model=model_id,
            action=action,
            exc_info=True,
        )


# ── LLM callback (replaces llm/logging.py) ──


def create_llm_log_callback(
    session_factory: async_sessionmaker[AsyncSession],
) -> LLMLogCallback:
    """Create a LogCallback for ModelRouter.

    Reads tenant_id from ContextVar at call time (not at creation time).
    """

    async def _log(
        response: LLMResponse,
        success: bool,
        error_message: str | None,
    ) -> None:
        await _persist(
            session_factory,
            tenant_id=None,  # resolved from contextvar
            action=response.action,
            strategy=response.strategy,
            provider=response.provider,
            model_id=response.model_id,
            unit_type="tokens",
            unit_in=response.tokens_in,
            unit_out=response.tokens_out,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
            success=success,
            error_message=error_message,
        )

    return _log


# Type alias matching ModelRouter's LogCallback signature
from collections.abc import Awaitable, Callable  # noqa: E402

LLMLogCallback = Callable[["LLMResponse", bool, str | None], Awaitable[None]]


# ── STT callback ──


def create_stt_log_callback(
    session_factory: async_sessionmaker[AsyncSession],
) -> STTLogCallback:
    """Create a LogCallback for STTRouter."""

    async def _log(
        result: STTResult | None,
        error_message: str | None,
    ) -> None:
        if result is None:
            return
        await _persist(
            session_factory,
            tenant_id=None,
            action=result.action,
            strategy=result.strategy,
            provider=result.provider,
            model_id=result.model_id,
            unit_type="minutes",
            unit_in=(
                round(result.audio_duration_sec) if result.audio_duration_sec else None
            ),
            unit_out=None,
            latency_ms=result.latency_ms,
            cost_usd=result.cost_usd,
            success=error_message is None,
            error_message=error_message,
        )

    return _log


STTLogCallback = Callable[["STTResult | None", str | None], Awaitable[None]]
