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

import hashlib
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime
from decimal import Decimal
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
    from course_supporter.llm.pricing import PricedCall
    from course_supporter.review_metrics import ReviewMetrics
    from course_supporter.storage.orm import ExternalServiceCall
    from course_supporter.stt.router import STTAttempt

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
so: a row is dropped when the call happens outside a job context, and when the
database write itself fails. Since BIL-C each drop is an ERROR line carrying
every field of the lost row (:func:`_row_fields`), so the row can be rebuilt
from the log; this count is the process-wide tally beside it. Read it with
:func:`skipped_write_counts`.
"""

_dropped_paid_calls = 0
"""How many rows of PAID calls were dropped (BIL-C, decision 6).

The test suite's lock reads it: every paid call must leave its row, so a test
that makes one outside a job context fails (``tests/conftest.py``). Never
reset — the lock compares it before and after each test.
"""


def _count_skipped_write(reason: str, *, paid: bool) -> int:
    """Count one dropped register row and return the running total for its kind.

    Counted here, REPORTED by the caller on the line it already writes: one
    event deserves one log line, and a second line beside it would be noise a
    reader has to learn to ignore.
    """
    global _dropped_paid_calls
    _skipped_writes[reason] += 1
    if paid:
        _dropped_paid_calls += 1
    return _skipped_writes[reason]


def skipped_write_counts() -> dict[str, int]:
    """A snapshot of the dropped-row counts, by reason (``DD-SP-AN``).

    For whoever reads a cost sum and needs to know whether it is complete.
    """
    return dict(_skipped_writes)


def dropped_paid_call_count() -> int:
    """How many rows of paid calls this process has dropped (BIL-C)."""
    return _dropped_paid_calls


def reset_skipped_write_counts() -> None:
    """Clear the per-reason counts. For tests; nothing in production resets them."""
    _skipped_writes.clear()


def _loggable(value: object) -> object:
    """A column value as a log line can carry it."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_fields(record: ExternalServiceCall) -> dict[str, object]:
    """Every field of a register row, for the ERROR line that replaces it.

    The two text bodies are replaced by their length and SHA-256: they can
    carry a student's submission or a model's words about it, which do not
    belong in a log (BIL-C, decision P6). Everything a sum is computed from is
    kept, so a dropped row can be rebuilt from the line.
    """
    fields: dict[str, object] = {}
    for column in record.__table__.columns:
        if column.name in ("input_text", "output_text"):
            body = getattr(record, column.name)
            fields[f"{column.name}_length"] = None if body is None else len(body)
            fields[f"{column.name}_sha256"] = (
                None if body is None else hashlib.sha256(body.encode()).hexdigest()
            )
            continue
        fields[f"row_{column.name}"] = _loggable(getattr(record, column.name))
    return fields


def _under_current_job(record: ExternalServiceCall) -> bool:
    """Stamp the row with the current job, or say loudly that it is dropped.

    ``job_id`` is the register's only mandatory FK (KD5); it comes from the
    contextvar set at the ARQ task entry. Outside a job the row cannot be
    written: that is an ERROR with every field of the row, never a quiet skip
    (BIL-C, decision 6). Production runtime is still never blocked through a
    telemetry hole.
    """
    job_id = get_current_job_id()
    if job_id is not None:
        record.job_id = job_id
        return True
    logger.error(
        "esc_write_skipped_no_job_id",
        # DD-SP-AN: a cost sum read out of the register can be short
        # without anything saying so. This is what says so.
        skip_reason="no_job_context",
        skipped_so_far=_count_skipped_write(
            "no_job_context", paid=record.cost_kind is not None
        ),
        **_row_fields(record),
    )
    return False


def add_register_row(
    session: AsyncSession,
    *,
    action: str,
    strategy: str,
    provider: str,
    model_id: str,
    latency_ms: int,
    success: bool,
    error_message: str | None,
    outcome: CallOutcome,
) -> None:
    """Stage a register row for a call that costs nothing, in the caller's session.

    For webhook deliveries: their row is committed with the caller's own
    transaction, as before, but it goes through the same job check as
    :func:`_persist` — a delivery outside a job context is an ERROR line, not a
    NOT NULL violation that would take the caller's transaction down with it.
    """
    from course_supporter.storage.orm import ExternalServiceCall

    record = ExternalServiceCall(
        action=action,
        strategy=strategy,
        provider=provider,
        model_id=model_id,
        latency_ms=latency_ms,
        success=success,
        error_message=error_message,
        outcome=outcome,
    )
    if _under_current_job(record):
        session.add(record)


async def _persist(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    action: str,
    strategy: str,
    provider: str | None,
    model_id: str | None,
    unit_type: str | None = None,
    unit_in: int | None = None,
    unit_in_cached: int | None = None,
    unit_in_estimate: int | None = None,
    unit_out: int | None = None,
    unit_out_reasoning: int | None = None,
    latency_ms: int | None = None,
    priced: PricedCall | None = None,
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

    ``priced`` is the price of a paid call (``llm/pricing.py``) and fills every
    price column at once: sum, kind, catalog version, tariff window, the
    instant the attempt was sent and the applied unit prices. A row without it
    records no paid call and carries none of them (BIL-C).

    ``job_id`` comes from the job context (:func:`_under_current_job`):
    outside one, and when the database write fails, the row is dropped with an
    ERROR line carrying all its fields (BIL-C, decision 6).
    """
    from course_supporter.storage.orm import ExternalServiceCall

    record = ExternalServiceCall(
        action=action,
        strategy=strategy,
        provider=provider,
        model_id=model_id,
        unit_type=unit_type,
        unit_in=unit_in,
        unit_in_cached=unit_in_cached,
        unit_in_estimate=unit_in_estimate,
        unit_out=unit_out,
        unit_out_estimate=priced.unit_out_estimate if priced else None,
        unit_out_reasoning=unit_out_reasoning,
        latency_ms=latency_ms,
        cost_usd=priced.cost_usd if priced else None,
        cost_kind=priced.cost_kind if priced else None,
        catalog_version=priced.catalog_version if priced else None,
        tariff_window=priced.tariff_window if priced else None,
        call_started_at=priced.call_started_at if priced else None,
        price_in_per_1k_usd=priced.price_in_per_1k_usd if priced else None,
        price_in_cached_per_1k_usd=(
            priced.price_in_cached_per_1k_usd if priced else None
        ),
        price_out_per_1k_usd=priced.price_out_per_1k_usd if priced else None,
        price_per_minute_usd=priced.price_per_minute_usd if priced else None,
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
        # NUMERIC column: the float the port speaks in, as the decimal it
        # prints as (BIL-C, decision P5).
        ceiling_estimate_usd=(
            Decimal(repr(ceiling_estimate_usd))
            if ceiling_estimate_usd is not None
            else None
        ),
        funds_decision=funds_decision,
        funds_refusal_reason=funds_refusal_reason,
        path_key=path_key,
    )
    if not _under_current_job(record):
        return
    try:
        async with session_factory() as session:
            session.add(record)
            await session.commit()
    except (SQLAlchemyError, OSError) as exc:
        logger.error(
            "service_call_log_failed",
            error=str(exc),
            # DD-SP-AN, the other way a row is dropped.
            skip_reason="database_error",
            skipped_so_far=_count_skipped_write(
                "database_error", paid=record.cost_kind is not None
            ),
            exc_info=True,
            **_row_fields(record),
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


# ── STT callback ──


def create_stt_log_callback(
    session_factory: async_sessionmaker[AsyncSession],
) -> STTLogCallback:
    """Create the register callback for STTRouter: one row per attempt.

    The router prices the attempt (``llm/pricing.py``) from the audio duration
    known before the call and hands the finished record over; this callback
    only writes it.
    """

    async def _log(attempt: STTAttempt) -> None:
        priced = attempt.priced
        await _persist(
            session_factory,
            action=attempt.action,
            strategy=attempt.strategy,
            provider=attempt.provider,
            model_id=attempt.model_id,
            unit_type="seconds",
            unit_in=priced.billed_seconds,
            unit_out=None,
            latency_ms=attempt.latency_ms,
            priced=priced,
            success=attempt.error_message is None,
            error_message=attempt.error_message,
            # A transcription has no response body to judge: only transport
            # success or failure apply.
            outcome=(
                CallOutcome.SUCCESS
                if attempt.error_message is None
                else CallOutcome.TRANSPORT_ERROR
            ),
        )

    return _log


STTLogCallback = Callable[["STTAttempt"], Awaitable[None]]
