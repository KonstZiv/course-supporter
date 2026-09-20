"""Unified logging for all external service calls (LLM, STT, VD).

Uses a ``ContextVar`` to propagate ``tenant_id`` from the ARQ task level
down to every external call, regardless of the service type.

Usage in task functions::

    from course_supporter.service_logging import set_tenant_from_job

    async def arq_ingest_material(ctx, ...):
        await set_tenant_from_job(session_factory, job_id)
        ...  # all LLM/STT/VD calls get tenant_id from contextvar

Factory functions create typed callbacks compatible with
StageRouter, STTRouter, and VD pipeline components.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.exc import SQLAlchemyError

from course_supporter.call_outcome import CallOutcome
from course_supporter.funds_port import FUNDS_PORT_ACTION
from course_supporter.review_metrics import REVIEW_METRICS_ACTION

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.call_outcome import FundsDecision, SkipReason
    from course_supporter.funds_port import FundsAnswer, FundsRefusalReason
    from course_supporter.homework.path_config import PathKey
    from course_supporter.llm.finish_reason import FinishReason
    from course_supporter.llm.schemas import LLMResponse
    from course_supporter.review_metrics import ReviewMetrics
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


# ── Stage-progress writer (worker → CPU-bound ingestion stages) ──

# An async ``(current, total) -> None`` closure the ARQ worker installs so a
# CPU-bound stage (video detection) can report per-frame progress without
# importing the DB/session layer — it only calls the opaque writer. The
# closure owns the session factory + job_id; ``None`` means "no reporting"
# (tests, direct calls). Best-effort: the writer swallows its own errors.
ProgressWriter = Callable[[int, int], Coroutine[Any, Any, None]]
_current_progress_writer: ContextVar[ProgressWriter | None] = ContextVar(
    "current_progress_writer", default=None
)


def set_progress_writer(
    writer: ProgressWriter | None,
) -> Token[ProgressWriter | None]:
    """Install a stage-progress writer for the current task; return a reset token."""
    return _current_progress_writer.set(writer)


def reset_progress_writer(token: Token[ProgressWriter | None]) -> None:
    """Reset the stage-progress writer to its previous value."""
    _current_progress_writer.reset(token)


def get_progress_writer() -> ProgressWriter | None:
    """Read the current stage-progress writer from context (``None`` if unset)."""
    return _current_progress_writer.get()


# ── Persist to DB ──


_skipped_writes: Counter[str] = Counter()
"""How many register rows were dropped, by why (``DD-SP-AN``).

A sum of costs read out of the register can be short without anything saying
so: a row is silently dropped when the call happens outside a job context, and
when the database write itself fails. Emptiness then looks exactly like a cheap
run — the same shape of lie as a test that is green for the wrong reason.

This is the visible trace: a process-wide count per reason, beside a log line
that names the same reason. It does not make the sum right — that is the debt
itself — but it lets a reader of the sum tell "it was cheap" from "it was not
all written down". Read it with :func:`skipped_write_counts`.
"""


def _count_skipped_write(reason: str) -> int:
    """Count one dropped register row and return the running total for its kind.

    Counted here, REPORTED by the caller on the line it already writes: one
    event deserves one log line, and a second line beside it would be noise a
    reader has to learn to ignore.
    """
    _skipped_writes[reason] += 1
    return _skipped_writes[reason]


def skipped_write_counts() -> dict[str, int]:
    """A snapshot of the dropped-row counts, by reason (``DD-SP-AN``).

    For whoever reads a cost sum and needs to know whether it is complete.
    """
    return dict(_skipped_writes)


def reset_skipped_write_counts() -> None:
    """Clear the counts. For tests; nothing in production resets them."""
    _skipped_writes.clear()


async def _persist(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    action: str,
    strategy: str,
    provider: str | None,
    model_id: str | None,
    unit_type: str | None = None,
    unit_in: int | None = None,
    unit_out: int | None = None,
    unit_out_reasoning: int | None = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    success: bool | None = True,
    error_message: str | None = None,
    outcome: CallOutcome | None = None,
    finish_reason: FinishReason | None = None,
    skip_reason: SkipReason | None = None,
    prompt_ref: str | None = None,
    prompt_hash: str | None = None,
    input_hash: str | None = None,
    input_text: str | None = None,
    output_text: str | None = None,
    authenticity: float | None = None,
    completeness: float | None = None,
    ceiling_estimate_usd: float | None = None,
    funds_decision: FundsDecision | None = None,
    funds_refusal_reason: FundsRefusalReason | None = None,
    path_key: str | None = None,
) -> None:
    """Write a single ExternalServiceCall row.

    DB errors are swallowed — call flow is never interrupted.

    ``success`` is ``None`` only on rows that record no call: a ladder trace
    (``outcome`` skipped / abandoned), the per-review metrics row and the
    funds-port row. The last two leave ``provider`` and ``model_id`` ``None``
    too; a trace names the rung it skipped or abandoned. Column meanings:
    ``storage.orm.ExternalServiceCall``.

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
            # DD-SP-AN: a cost sum read out of the register can be short
            # without anything saying so. This is what says so.
            skip_reason="no_job_context",
            skipped_so_far=_count_skipped_write("no_job_context"),
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
        unit_out_reasoning=unit_out_reasoning,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        success=success,
        error_message=error_message,
        outcome=outcome,
        finish_reason=finish_reason,
        skip_reason=skip_reason,
        prompt_ref=prompt_ref,
        prompt_hash=prompt_hash,
        input_hash=input_hash,
        input_text=input_text,
        output_text=output_text,
        authenticity=authenticity,
        completeness=completeness,
        ceiling_estimate_usd=ceiling_estimate_usd,
        funds_decision=funds_decision,
        funds_refusal_reason=funds_refusal_reason,
        path_key=path_key,
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
            # DD-SP-AN, the other way a row is dropped.
            skip_reason="database_error",
            skipped_so_far=_count_skipped_write("database_error"),
            exc_info=True,
        )


# ── Review metrics row ──


async def record_review_metrics(
    session_factory: async_sessionmaker[AsyncSession],
    metrics: ReviewMetrics,
) -> None:
    """Write the per-review metrics row to the call register.

    Takes finished numbers, never a calculator: swapping the metrics formula
    (``review_metrics.ReviewMetricsCalculator``) cannot change where or how
    they are stored. The row records no call — ``provider``, ``model_id``,
    ``success``, ``outcome`` and ``cost_usd`` are all NULL — and is found by
    ``action = 'review_metrics'`` (its metric columns are NULL too when the
    review made no claims). It inherits
    :func:`_persist`'s contract: written only inside a job context, DB errors
    swallowed.
    """
    await _persist(
        session_factory,
        action=REVIEW_METRICS_ACTION,
        strategy="default",
        provider=None,
        model_id=None,
        success=None,
        authenticity=metrics.authenticity,
        completeness=metrics.completeness,
    )


# ── Funds-port row ──


async def record_funds_decision(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    path_key: PathKey | None,
    ceiling_estimate_usd: float,
    answer: FundsAnswer,
) -> None:
    """Write the funds-port row: a path's ceiling estimate and the port's answer.

    Takes the finished answer, never a port: whichever implementation decided,
    the numbers are stored the same way. The path key is stored as its text
    (``task/first``). The row records no call — ``provider``, ``model_id``,
    ``success``, ``cost_usd`` and ``outcome`` are all NULL — and is found by
    ``action = 'funds_port'``. A refusal's reason goes to
    ``funds_refusal_reason`` and never to ``error_message``: a refusal is an
    answer, not a failed call, and the canonical failure count
    (``NOT success OR error_message IS NOT NULL``) must not count it. It
    inherits :func:`_persist`'s contract: written only inside a job context,
    DB errors swallowed.
    """
    await _persist(
        session_factory,
        action=FUNDS_PORT_ACTION,
        strategy="default",
        provider=None,
        model_id=None,
        success=None,
        ceiling_estimate_usd=ceiling_estimate_usd,
        funds_decision=answer.decision,
        funds_refusal_reason=answer.refusal_reason,
        path_key=str(path_key) if path_key is not None else None,
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
                    outcome=CallOutcome.TRANSPORT_ERROR,
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
            # A transcription has no response body to judge: only transport
            # success or failure apply.
            outcome=(
                CallOutcome.SUCCESS
                if error_message is None
                else CallOutcome.TRANSPORT_ERROR
            ),
        )

    return _log


STTLogCallback = Callable[["STTResult | None", str | None], Awaitable[None]]
