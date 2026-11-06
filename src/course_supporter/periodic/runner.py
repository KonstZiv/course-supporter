"""How one periodic task runs: its lock, its transaction, its one log line.

A periodic task is a :class:`PeriodicTask`: a name and a body. :func:`run_task`
runs the body and NEVER raises (except for a cancellation, which it reports and
passes on): whatever happens, it writes one structured line,
``periodic_task_run``, with the task, the rows processed, the duration and
the error, if any. So a failing task cannot stop the worker or another task.

Transactions and locks:
    The body opens its transactions through :meth:`TaskContext.transaction`.
    Each one first takes a Postgres advisory lock named after the task
    (``pg_try_advisory_xact_lock``); when another transaction of the same task
    holds it — the scheduler on another worker, a manual run of
    ``scripts/billing_admin.py run-periodic`` — the run is skipped, not
    queued behind it. The lock is transaction-scoped: it is released by the
    commit or the rollback, and by Postgres when a connection dies, so a
    crashed run never leaves it held. A dry run rolls its transaction back.

    The lock only spares duplicate work: each billing body is safe without it,
    because every change of units locks the account's row and re-reads it
    (``billing.journal.lock_account``), so a second run finds nothing to do.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from course_supporter.config import Settings

log = structlog.get_logger()


class TaskBusyError(Exception):
    """Another transaction of the same task holds its lock."""


@dataclass(frozen=True, slots=True)
class TaskResult:
    """What a body did."""

    processed: int
    """Rows processed: movements written, reserves released, rows deleted."""
    details: Mapping[str, int] = field(default_factory=dict)
    """Further counts for the log line (units, accounts …)."""


@dataclass(frozen=True, slots=True)
class TaskContext:
    """What a body is given."""

    task_name: str
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    now: datetime
    dry_run: bool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """A transaction holding the task's lock; committed (rolled back on a
        dry run or an error) when the block ends.

        Raises:
            TaskBusyError: another transaction of the task holds the lock.
        """
        async with self.session_factory() as session:
            locked = await session.scalar(
                select(func.pg_try_advisory_xact_lock(lock_key(self.task_name)))
            )
            if not locked:
                raise TaskBusyError(self.task_name)
            yield session
            if self.dry_run:
                await session.rollback()
            else:
                await session.commit()


TaskBody = Callable[[TaskContext], Awaitable[TaskResult]]


@dataclass(frozen=True, slots=True)
class PeriodicTask:
    """A task the scheduler runs (``course_supporter.periodic.registry``)."""

    name: str
    """Unique; names the ARQ job (``periodic:<name>``) and the lock."""
    body: TaskBody
    enabled: Callable[[Settings], bool]
    """Its switch in the settings."""
    cron: Callable[[Settings], str]
    """Its schedule in the settings (``periodic.schedule``)."""
    summary: str
    """One line for ``--help`` and the docs."""


@dataclass(frozen=True, slots=True)
class TaskRun:
    """The outcome of one run, as logged."""

    task: str
    processed: int
    details: Mapping[str, int]
    duration_ms: int
    dry_run: bool
    skipped: str | None = None
    """``busy`` when another run held the lock."""
    error: str | None = None


def lock_key(task_name: str) -> int:
    """The task's advisory lock: a stable signed 64-bit number from its name."""
    digest = hashlib.sha256(f"periodic:{task_name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def run_task(
    task: PeriodicTask,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> TaskRun:
    """Run the task's body once and log one line (module docstring)."""
    if settings is None:
        from course_supporter.config import get_settings

        settings = get_settings()
    context = TaskContext(
        task_name=task.name,
        session_factory=session_factory,
        settings=settings,
        now=now or datetime.now(UTC),
        dry_run=dry_run,
    )
    started = time.monotonic()

    def outcome(
        result: TaskResult | None,
        *,
        skipped: str | None = None,
        error: str | None = None,
    ) -> TaskRun:
        return TaskRun(
            task=task.name,
            processed=result.processed if result else 0,
            details=dict(result.details) if result else {},
            duration_ms=round((time.monotonic() - started) * 1000),
            dry_run=dry_run,
            skipped=skipped,
            error=error,
        )

    try:
        run = outcome(await task.body(context))
    except TaskBusyError:
        run = outcome(None, skipped="busy")
    except asyncio.CancelledError:
        _log(outcome(None, error="cancelled"), failed=True)
        raise
    except Exception as exc:
        run = outcome(None, error=f"{type(exc).__name__}: {exc}")
        _log(run, failed=True)
        return run
    _log(run, failed=False)
    return run


def _log(run: TaskRun, *, failed: bool) -> None:
    fields = {
        "task": run.task,
        "processed": run.processed,
        "duration_ms": run.duration_ms,
        "dry_run": run.dry_run,
        "skipped": run.skipped,
        "error": run.error,
        **run.details,
    }
    if failed:
        log.error("periodic_task_run", exc_info=True, **fields)
    else:
        log.info("periodic_task_run", **fields)
