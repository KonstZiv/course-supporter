"""Concrete on_cancel_jobs implementation for CascadeDeleteService.

Vision §3 KD13 + KD12. When a soft-delete cascade fires the
``on_cancel_jobs`` hook with the collected victim ids, this service
finds in-flight Jobs whose **subject** is one of those victims and
flips them to ``status='cancelled'`` in the same DB transaction (via
the status owner, :meth:`JobRepository.update_status`). Workers then
observe the new status at their next ``current_stage`` boundary via
:class:`JobCancellationChecker` and exit gracefully.

**L1b — one predicate arm.** The lookup is a single
``Job.subject_id.in_(victim_ids)`` (plus the in-flight + not-soft-deleted
filters). The cascade hands the FULL victim id set — every soft-deleted
entity of every type, since the cascade engine does not filter by type —
so a document-subject job is matched by its AuthoredDocument id and a
node-subject job by its CourseNode id, both already present in the victim
set (H-L1b-4 Case A). This replaced a three-arm OR
(``tenant_id`` / ``course_node_id`` / ``input_params @> {course_node_id}``)
that keyed on the node: it over-cancelled every sibling job of the node
(F3) and could not see a document subject at all (F2) — the reason each
document-rooted caller had to inject ``course_node_id`` (the "augmented"
hack, now removed). The ``tenant_id`` arm was unreachable (no caller roots
a cascade on a Tenant); the JSONB arm was dead against real data (no writer
ever put ``course_node_id`` into ``input_params``) — so retiring it drops
the deferred GIN-index debt (DD-L1a-D) as no-longer-applicable rather than
implementing it.

Phase 1+ entity tasks bind this as the :data:`OnCancelJobs`
callable on their cascade declarations::

    jcs = JobCancellationService(session)
    await cascade_service.soft_delete_with_cascade(
        entity,
        cascade_map,
        on_cancel_jobs=jcs.cancel_jobs_for_entities,
    )
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
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
        """Cancel all in-flight Jobs whose subject is one of ``entity_ids``.

        ``entity_ids`` is the cascade's full victim id set (every
        soft-deleted entity of every type). A Job is cancelled when its
        typed ``subject_id`` (L1b) is in that set — precise per-subject
        cancellation with no over-cancel: deleting one document no longer
        touches its siblings' jobs. Node deletion still cancels the whole
        subtree because the victim set already contains the subtree's
        AuthoredDocument and CourseNode ids (H-L1b-4 Case A).

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

        # L1b: one arm — match the typed subject against the victim id set.
        # NULL subject_id (s3_cleanup / unrecoverable historical) matches
        # nothing, which is correct: those jobs have no subject to cancel by.
        stmt = (
            select(Job)
            .where(Job.status.in_(IN_FLIGHT_STATUSES))
            .where(Job.deleted_at.is_(None))
            .where(Job.subject_id.in_(entity_ids))
        )
        result = await self._session.execute(stmt)
        jobs = list(result.scalars().all())

        # Route every cancel through the status owner (contract §3
        # "Ownership"): ``active -> cancelled`` is a legal transition and
        # ``update_status`` stamps ``completed_at`` as the termination time.
        # N per-row calls; the width is node-subtree-bounded.
        repo = JobRepository(self._session)
        for job in jobs:
            await repo.update_status(job.id, "cancelled", now=ts)

        logger.info(
            "job_cancellation_service.cancelled",
            entity_count=len(entity_ids),
            cancelled_count=len(jobs),
        )
