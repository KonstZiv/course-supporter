"""Cooperative Job cancellation polling for ARQ workers (vision §3 KD13).

KD13 cancel-flow:

1. Cascade transaction sets ``Job.status``: ``queued/running →
   cancelled`` in the same DB transaction as ``deleted_at`` (the
   actual flip lives in :class:`JobCancellationService`, commit (e)
   of task 0.3).
2. Pipeline workers poll ``status`` at every ``current_stage``
   boundary via :meth:`JobCancellationChecker.raise_if_cancelled`;
   on observation, they release in-flight resources, write a
   checkpoint to ``Job.stage_progress`` (per KD4a), and exit
   gracefully — no new ``deleted_at IS NOT NULL`` UPDATEs (which
   the soft-delete trigger from task 0.1 would block anyway).

This is **cooperative**, not native ARQ cancel. ARQ's hard-kill
abort_job aborts the coroutine mid-await, skipping any cleanup the
stage was about to do (release LLM client connections, finalize
ESC rows, persist a partial checkpoint). Cooperative polling
trades a small latency tax (one ``SELECT status`` per stage
boundary, ≪ pipeline cost) for a clean exit window.

**Worker integration is documented but NOT wired in this commit.**
Phase 2.x rewrites the pipeline workers (``arq_ingest_material``,
``arq_execute_methodist_step``, etc.) to call
``await checker.raise_if_cancelled(job_id)`` between stages and
to wrap the call in a specific ``except JobCancelledError`` that
runs cleanup + returns gracefully (without flipping the Job to
``failed``). The contract is fixed here so the rewrite has a
stable API to integrate against.

**Catch-order contract for workers:**

```python
try:
    await pipeline_stage(...)
    await checker.raise_if_cancelled(job_id)
except JobCancelledError:
    await release_resources()
    await persist_checkpoint(stage_progress)
    return  # do NOT flip status to failed
except Exception:
    await mark_job_failed(...)
    raise
```

The :class:`JobCancelledError` clause MUST appear before the generic
``except Exception`` block. Reversing the order would mark a
deliberately-cancelled Job as ``failed`` — incorrect bookkeeping
and noise for cost reports.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import Job


class JobCancelledError(Exception):
    """Raised by :class:`JobCancellationChecker` when a Job is cancelled.

    Subclasses :class:`Exception` (not :class:`BaseException`).
    Cooperative cancellation is anticipated control flow, not a runtime
    bug, so a regular exception is the honest signal type. The trade-off
    is that a generic ``except Exception:`` will catch it — workers MUST
    have a dedicated ``except JobCancelledError:`` clause earlier in the
    handler chain (see module docstring "Catch-order contract").

    Carries ``job_id`` and a short ``reason`` string distinguishing the
    two cancel-equivalent paths:

    * ``"status=cancelled"`` — cascade transaction observed; KD13's
      Path-1/Path-2 cancellation has fired.
    * ``"row not found"`` — Job row absent from the table. Treated as
      cancelled per the same "release resources + exit" semantics; see
      :class:`JobCancellationChecker` for the design rationale.

    The ``reason`` is an operator-facing diagnostic (logs, error
    messages); workers should not branch on its value.
    """

    def __init__(self, job_id: uuid.UUID, reason: str) -> None:
        self.job_id = job_id
        self.reason = reason
        super().__init__(f"Job {job_id} cancelled ({reason})")


class JobCancellationChecker:
    """Poll ``Job.status`` for cooperative cancellation.

    Caller controls the session lifecycle — the recommended pattern
    is a short-lived read-only session per check, separate from the
    main pipeline transaction, so the polling SELECT does not
    interfere with the worker's own write transaction.

    Soft-deleted Jobs stay queryable: the soft-delete trigger from
    task 0.1 blocks UPDATEs, not SELECTs, so a Job with both
    ``status='cancelled'`` and ``deleted_at IS NOT NULL`` (the
    end-state of a cascade) is still observable. ``is_cancelled`` /
    ``raise_if_cancelled`` therefore intentionally do **not** filter
    on ``deleted_at``: filtering would hide legitimately cancelled
    rows in their soft-deleted final state.

    **Missing Job is treated as cancelled.** When the SELECT returns
    no row, both methods report cancellation. Operationally this is
    the right answer — the worker has nothing left to write back
    and should release resources and exit, exactly the cancellation
    behavior. Forcing callers to handle a separate "row not found"
    error would multiply the same handler logic. Transient DB
    failures (connection drops, server restarts) raise distinct
    psycopg / SQLAlchemy exceptions and are not collapsed into
    cancellation.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_cancelled(self, job_id: uuid.UUID) -> bool:
        """Return ``True`` if the Job is cancelled or its row is missing."""
        status = await self._fetch_status(job_id)
        return status is None or status == "cancelled"

    async def raise_if_cancelled(self, job_id: uuid.UUID) -> None:
        """Raise :class:`JobCancelledError` if cancelled or missing.

        Intended as the boundary-call inside pipeline workers
        (Phase 2.x integration). See module docstring for the
        catch-order contract.
        """
        status = await self._fetch_status(job_id)
        if status is None:
            raise JobCancelledError(job_id, "row not found")
        if status == "cancelled":
            raise JobCancelledError(job_id, "status=cancelled")

    async def _fetch_status(self, job_id: uuid.UUID) -> str | None:
        """Return ``Job.status`` for ``job_id``, or ``None`` if no row."""
        stmt = select(Job.status).where(Job.id == job_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
