"""Unified logging for all external service calls (LLM, STT, VD).

Uses a ``ContextVar`` to propagate ``tenant_id`` from the ARQ task level
down to every external call, regardless of the service type.

Usage in task functions::

    from course_supporter.service_logging import set_tenant_from_job

    async def arq_ingest_material(ctx, ...):
        await set_tenant_from_job(session_factory, job_id)
        ...  # all LLM/STT/VD calls get tenant_id from contextvar

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

# ── Context variables for per-task tenant + job isolation ──

_current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar(
    "current_tenant_id", default=None
)
_current_job_id: ContextVar[uuid.UUID | None] = ContextVar(
    "current_job_id", default=None
)


@contextmanager
def tenant_scope(tenant_id: uuid.UUID) -> Iterator[None]:
    """Set tenant_id for the duration of an ARQ task."""
    token = _current_tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _current_tenant_id.reset(token)


@contextmanager
def job_scope(job_id: uuid.UUID) -> Iterator[None]:
    """Set job_id for the duration of an ARQ task (test/contract use)."""
    token = _current_job_id.set(job_id)
    try:
        yield
    finally:
        _current_job_id.reset(token)


def get_current_tenant_id() -> uuid.UUID | None:
    """Read the current tenant_id from context."""
    return _current_tenant_id.get()


def get_current_job_id() -> uuid.UUID | None:
    """Read the current job_id from context."""
    return _current_job_id.get()


async def set_tenant_from_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
) -> None:
    """Look up tenant_id from Job and set it in contextvar for cost logging.

    Always resets the contextvar first to prevent tenant leakage
    between sequential ARQ tasks in the same worker.
    Best-effort: DB/network failures are logged but never propagate.
    """
    from course_supporter.storage.job_repository import JobRepository

    _current_tenant_id.set(None)
    try:
        async with session_factory() as session:
            job = await JobRepository(session).get_by_id(job_id)
            if job and job.tenant_id:
                _current_tenant_id.set(job.tenant_id)
    except (SQLAlchemyError, OSError) as exc:
        logger.warning(
            "tenant_lookup_failed",
            job_id=str(job_id),
            error=str(exc),
            exc_info=True,
        )
    except (TypeError, AttributeError) as exc:
        logger.error(
            "tenant_lookup_unexpected_error",
            job_id=str(job_id),
            error=str(exc),
            exc_info=True,
        )


def set_job_from_arq(job_id: uuid.UUID) -> None:
    """Pure setter — job_id is known at ARQ task entry, no DB lookup needed.

    Mirrors ``set_tenant_from_job`` contract but skips DB IO; the caller
    already has the validated UUID from the ARQ task signature. Always
    overwrites — prevents leakage between sequential ARQ tasks reusing
    the same worker.
    """
    _current_job_id.set(job_id)


# ── Persist to DB ──


async def _persist(
    session_factory: async_sessionmaker[AsyncSession],
    *,
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

    Two-layer guard for ``job_id`` (KD5 — only mandatory FK):
    1. Read ``job_id`` from contextvar (set at ARQ task entry).
    2. If absent, log WARNING and skip the write — would otherwise
       violate the post-0.4 ``NOT NULL`` constraint. Production runtime
       is never blocked through a telemetry hole.
    """
    from course_supporter.storage.orm import ExternalServiceCall

    job_id = get_current_job_id()
    if job_id is None:
        logger.warning(
            "esc_write_skipped_no_job_id",
            action=action,
            provider=provider,
            model_id=model_id,
        )
        return

    record = ExternalServiceCall(
        job_id=job_id,
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
    except (SQLAlchemyError, OSError) as exc:
        logger.error(
            "service_call_log_failed",
            provider=provider,
            model=model_id,
            action=action,
            error=str(exc),
            exc_info=True,
        )


# ── LLM callback ──


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
            if error_message:
                await _persist(
                    session_factory,
                    action="transcribe",
                    strategy="unknown",
                    provider="unknown",
                    model_id="unknown",
                    success=False,
                    error_message=error_message,
                )
            return
        await _persist(
            session_factory,
            action=result.action,
            strategy=result.strategy,
            provider=result.provider,
            model_id=result.model_id,
            unit_type="seconds",
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
