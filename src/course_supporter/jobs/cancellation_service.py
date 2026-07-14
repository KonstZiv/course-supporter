"""Concrete on_cancel_jobs implementation for CascadeDeleteService.

Vision §3 KD13 + KD12. When a soft-delete cascade fires the
``on_cancel_jobs`` hook with the collected victim ids, this service
finds active Jobs that reference any of those entities — by
``tenant_id``, ``course_node_id``, or ``input_params`` JSONB
containment — and flips them to ``status='cancelled'`` in the same
DB transaction. Workers then observe the new status at their next
``current_stage`` boundary via :class:`JobCancellationChecker`
(commit (d)) and exit gracefully.

Phase 1+ entity tasks bind this as the :data:`OnCancelJobs`
callable on their cascade declarations::

    jcs = JobCancellationService(session)
    await cascade_service.soft_delete_with_cascade(
        entity,
        cascade_map,
        on_cancel_jobs=jcs.cancel_jobs_for_entities,
    )

0.3 ships the implementation only — no ``__cascades_soft_delete_to__``
list is populated here, no entity-specific binding lands.

**Performance note for POST-MR-NOTES.** The JSONB containment branch
generates one ``Job.input_params @> '{"course_node_id": ...}'`` clause
per victim id. PostgreSQL's ``@>`` operator does not natively check
"any of these values for the same key", so OR-of-N is the correct
shape. A GIN index on ``Job.input_params`` (``CREATE INDEX
ix_jobs_input_params_gin ON jobs USING GIN (input_params jsonb_path_ops)``)
would convert each ``@>`` from a sequential scan to an index-only
lookup, but adding it now is premature: the migration is one round
trip per cascade in dev/test, and real cascade widths are not yet
known. Phase 2.x will add the GIN index once telemetry from
production cascades shows the JSONB branch as a real bottleneck —
ideally bundled with the call-site rewrite that drops the legacy
``input_params["course_node_id"]`` indirection in favour of the
direct FK.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import Job

logger = structlog.get_logger(__name__)


class JobCancellationService:
    """Concrete :data:`OnCancelJobs` implementation.

    Single-shot, single-transaction: the caller (typically
    :meth:`CascadeDeleteService.soft_delete_with_cascade` via the
    bound hook) controls the surrounding transaction. ``flush()``
    only happens inside; failure propagates and rolls back as part
    of the outer transaction. Mirrors the
    :data:`OnInvalidateHashes` shape shipped in 0.2.

    Idempotent on re-run: the status filter
    (:data:`~course_supporter.storage.job_repository.IN_FLIGHT_STATUSES`
    — ``queued`` / ``active``) excludes already-cancelled / completed
    Jobs, so a second invocation with the same ``entity_ids`` is a no-op
    other than the log line. The ``deleted_at IS NULL`` filter further
    guards against the soft-delete trigger from task 0.1 — without it, a
    Job that was already cancelled-and-soft-deleted in a prior cascade
    would raise on UPDATE (the trigger blocks any column change other
    than ``deleted_at`` itself on soft-deleted rows).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def cancel_jobs_for_entities(
        self,
        entity_ids: list[uuid.UUID],
        *,
        now: datetime | None = None,
    ) -> None:
        """Cancel all in-progress Jobs that reference any ``entity_id``.

        Lookup is OR-of-paths because Jobs reference entities through
        three different columns depending on ``job_type``:

        * ``Job.tenant_id`` — tenant-wide cascades.
        * ``Job.course_node_id`` — node-scoped jobs (KD13 canonical FK).
        * ``Job.input_params @> {"course_node_id": ...}`` — legacy /
          edge cases where the FK is NULL but the JSONB carries the id
          (some pre-rewrite orchestrators emit this shape).

        ``now`` overrides the ``completed_at`` timestamp for
        deterministic tests; defaults to ``datetime.now(UTC)``.
        Defaulted to ``None`` so the bound method matches the
        ``OnCancelJobs`` callable shape (``Callable[[list[UUID]],
        Awaitable[None]]``) — the cascade engine invokes it with
        only the positional ``entity_ids`` argument.
        """
        if not entity_ids:
            return

        # Lazy import breaks the jobs↔storage import cycle: ``jobs/__init__``
        # eagerly imports this module, while ``storage.job_repository``
        # imports the ``jobs`` package (JobType). A module-top import of the
        # repository here would close that cycle at import time.
        from course_supporter.storage.job_repository import (
            IN_FLIGHT_STATUSES,
            JobRepository,
        )

        ts = now if now is not None else datetime.now(UTC)

        jsonb_clauses = [
            Job.input_params.op("@>")({"course_node_id": str(eid)})
            for eid in entity_ids
        ]

        stmt = (
            select(Job)
            .where(Job.status.in_(IN_FLIGHT_STATUSES))
            .where(Job.deleted_at.is_(None))
            .where(
                or_(
                    Job.tenant_id.in_(entity_ids),
                    Job.course_node_id.in_(entity_ids),
                    *jsonb_clauses,
                )
            )
        )
        result = await self._session.execute(stmt)
        jobs = list(result.scalars().all())

        # Route every cancel through the status owner (contract §3
        # "Ownership"): ``active -> cancelled`` is now a legal transition and
        # ``update_status`` stamps ``completed_at`` as the termination time.
        # N per-row calls replace the prior in-loop attr-assign + single
        # flush; the width is node-subtree-bounded (the ``tenant_id`` OR-arm
        # is unreachable — no caller roots the cascade on a Tenant).
        repo = JobRepository(self._session)
        for job in jobs:
            await repo.update_status(job.id, "cancelled", now=ts)

        logger.info(
            "job_cancellation_service.cancelled",
            entity_count=len(entity_ids),
            cancelled_count=len(jobs),
        )
