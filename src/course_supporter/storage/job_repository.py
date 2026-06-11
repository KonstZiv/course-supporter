"""Repository for Job CRUD and status management."""

from __future__ import annotations

import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.jobs import JobType, validate_job_type
from course_supporter.storage.orm import CourseNode, Job

logger = structlog.get_logger()

# Valid job status transitions
JOB_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"active", "cancelled", "failed"},
    "active": {"complete", "failed"},
    "complete": set(),
    "failed": {"queued"},  # retry
    "cancelled": set(),
}


class JobRepository:
    """Repository for job tracking operations.

    Not tenant-scoped — jobs are accessed via node_id or directly by id.
    Tenant isolation is enforced via ``get_by_id_for_tenant`` which joins
    through ``CourseNode.tenant_id``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        tenant_id: uuid.UUID | None = None,
        course_node_id: uuid.UUID | None = None,
        job_type: JobType | str,
        priority: str = "normal",
        arq_job_id: str | None = None,
        input_params: dict[str, object] | None = None,
        depends_on: list[str] | None = None,
    ) -> Job:
        """Create a new job record.

        ``job_type`` accepts either a :class:`JobType` enum (KD13
        canonical, recommended for new code) or a legacy ``str``
        (transitional — Phase 2.x will rewrite call-sites). Legacy
        strings emit a one-shot :class:`DeprecationWarning` per
        distinct value via :func:`validate_job_type`. The strict
        DB CHECK constraint that would reject legacy values is
        deferred to Phase 2.x along with the call-site migration.
        """
        job = Job(
            tenant_id=tenant_id,
            course_node_id=course_node_id,
            job_type=validate_job_type(job_type),
            priority=priority,
            arq_job_id=arq_job_id,
            input_params=input_params,
            depends_on=depends_on,
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def get_by_id(self, job_id: uuid.UUID) -> Job | None:
        """Get a job by primary key."""
        stmt = select(Job).where(Job.id == job_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_status(
        self,
        job_id: uuid.UUID,
        status: str,
        *,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> Job:
        """Transition job to a new status with validation.

        Args:
            now: Override for current time (useful for testing).

        Raises:
            ValueError: If the transition is not allowed.
        """
        job = await self.get_by_id(job_id)
        if job is None:
            msg = f"Job {job_id} not found"
            raise ValueError(msg)

        # Idempotent no-op when target equals current. ARQ task replay
        # (worker restart mid-await, ``max_tries`` retry, manual rerun)
        # would otherwise raise on ``active → active`` and crash the
        # second attempt before the real work runs (TASK-2.4.18).
        # Mirrors ``update_stage`` idempotency.
        if job.status == status:
            return job

        allowed = JOB_TRANSITIONS.get(job.status, set())
        if status not in allowed:
            msg = (
                f"Invalid job status transition: '{job.status}' → '{status}'. "
                f"Allowed: {allowed or 'none (terminal state)'}"
            )
            raise ValueError(msg)

        now = now or datetime.now(UTC)
        values: dict[str, object] = {"status": status}

        if status == "active":
            values["started_at"] = now
        elif status in ("complete", "failed"):
            values["completed_at"] = now
            if error_message is not None:
                values["error_message"] = error_message

        stmt = update(Job).where(Job.id == job_id).values(**values)
        await self._session.execute(stmt)
        await self._session.flush()
        # Re-fetch to get updated state
        updated = await self.get_by_id(job_id)
        if updated is None:  # pragma: no cover — guaranteed by prior flush
            msg = f"Job {job_id} disappeared after update"
            raise RuntimeError(msg)
        return updated

    async def set_arq_job_id(self, job_id: uuid.UUID, arq_job_id: str) -> None:
        """Set the ARQ job identifier after enqueue."""
        stmt = update(Job).where(Job.id == job_id).values(arq_job_id=arq_job_id)
        await self._session.execute(stmt)
        await self._session.flush()

    async def update_stage(self, job_id: uuid.UUID, stage_name: str) -> None:
        """Update the ``current_stage`` checkpoint marker on a live Job.

        Atomic UPDATE filtered through ``Job.deleted_at IS NULL`` so
        soft-deleted Jobs are skipped silently (KD13 + Phase 1
        soft-delete cascade discipline). No status transition
        validation -- stage taxonomy is per-pipeline free-form per
        :class:`Job.current_stage` column docs.

        Silent skip on:

        * Job not found (no row matches ``job_id``).
        * Job soft-deleted (``deleted_at IS NOT NULL``).

        Both cases mirror :meth:`set_arq_job_id` / :meth:`store_result`
        convention (void return; no exception). A ``log.warning`` is
        emitted on zero-rowcount for observability of caller race or
        wrong-id bugs without breaking the contract.

        Production callers added in Phase 2.1 C5 (Pass 2a entry), C6
        (Stage 2 entry), C7 (Pass 2b entry) per KD-2.1-B. Stage names
        live in ``config/ladders_*.yaml`` (KD16); this method accepts
        loose ``str`` and does no validation at write-time.
        """
        stmt = (
            update(Job)
            .where(Job.id == job_id, Job.deleted_at.is_(None))
            .values(current_stage=stage_name)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        # ``rowcount`` is provided by CursorResult (actual runtime type
        # for DML execute); SQLAlchemy's static return type is the wider
        # ``Result`` which does not expose it -- hence the ignore.
        if (result.rowcount or 0) == 0:  # type: ignore[attr-defined]
            logger.warning(
                "update_stage_no_job_found",
                job_id=str(job_id),
                stage_name=stage_name,
            )

    async def update_stage_progress(
        self, job_id: uuid.UUID, payload: dict[str, Any]
    ) -> None:
        """Full-replace ``Job.stage_progress`` JSONB (KD13 checkpoint).

        Atomic UPDATE filtered through ``Job.deleted_at IS NULL`` (same
        contract as :meth:`update_stage`). Silent skip on missing /
        soft-deleted row + warn-log for observability.

        **Full-replace is safe only because callers are strictly
        sequential** (Phase 3.2.2 visits await one another; no
        ``asyncio.gather`` over visits). Parallel visits would need
        JSON-merge instead of replace — a separate design decision,
        not a refactor. Any future temptation to fan-out node visits
        must surface as STOP-escalate first (K3 ratify, Phase 3.2.2
        commit 2).
        """
        stmt = (
            update(Job)
            .where(Job.id == job_id, Job.deleted_at.is_(None))
            .values(stage_progress=payload)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        if (result.rowcount or 0) == 0:  # type: ignore[attr-defined]
            logger.warning(
                "update_stage_progress_no_job_found",
                job_id=str(job_id),
            )

    async def reactivate(self, job_id: uuid.UUID) -> Job:
        """Re-queue a failed Job for retry (vision §3 KD13).

        Allowed only from ``status='failed'`` on a non-soft-deleted Job.
        Other states (including soft-deleted) raise :class:`ValueError`;
        callers map to 4xx (404 for missing, 409 for wrong state).

        DB-side transition only — caller is responsible for enqueuing
        the new ARQ task and calling :meth:`set_arq_job_id` with the
        new id, all within the same outer transaction. Mirrors the
        :meth:`create` / :meth:`set_arq_job_id` split used by
        ``enqueue_ingestion`` and friends.

        Cleared on transition: ``status`` (→ ``'queued'``),
        ``error_message``, ``started_at``, ``completed_at``,
        ``arq_job_id`` (caller will set the new one).

        Preserved on transition:

        * ``queued_at`` — original first-queued time is more meaningful
          as Job metadata; per-attempt timing lives in
          ``ExternalServiceCall`` per KD5/KD13.
        * ``stage_progress`` — worker resumes from KD4a checkpoint.
          This preservation is the design intent of ``reactivate``
          (vs creating a new Job from scratch).
        """
        job = await self.get_by_id(job_id)
        if job is None:
            msg = f"Job {job_id} not found"
            raise ValueError(msg)
        if job.deleted_at is not None:
            msg = f"Cannot reactivate soft-deleted Job {job_id}"
            raise ValueError(msg)
        if job.status != "failed":
            msg = (
                f"Cannot reactivate Job {job_id} in state {job.status!r}; "
                f"only 'failed' Jobs can be reactivated."
            )
            raise ValueError(msg)
        job.status = "queued"
        job.error_message = None
        job.started_at = None
        job.completed_at = None
        job.arq_job_id = None
        await self._session.flush()
        return job

    async def store_result(
        self, job_id: uuid.UUID, result_data: dict[str, Any]
    ) -> None:
        """Store task result payload on the Job record."""
        stmt = update(Job).where(Job.id == job_id).values(result_data=result_data)
        await self._session.execute(stmt)
        await self._session.flush()

    async def get_by_id_for_tenant(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Job | None:
        """Get a job by ID, ensuring it belongs to the given tenant.

        Joins through ``job.course_node_id → material_node.tenant_id`` for
        isolation. Falls back to ``job.tenant_id`` for jobs without a
        linked node.
        """
        # Try node-based isolation first
        stmt = (
            select(Job)
            .join(CourseNode, Job.course_node_id == CourseNode.id)
            .where(Job.id == job_id, CourseNode.tenant_id == tenant_id)
        )
        result = await self._session.execute(stmt)
        job = result.scalar_one_or_none()
        if job is not None:
            return job

        # Fallback: direct tenant_id on job
        stmt = select(Job).where(Job.id == job_id, Job.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_for_node(self, node_id: uuid.UUID) -> list[Job]:
        """Get all active (queued or running) jobs for a node."""
        stmt = (
            select(Job)
            .where(
                Job.course_node_id == node_id,
                Job.status.in_(["queued", "active"]),
            )
            .order_by(Job.queued_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_generation_jobs(self, node_id: uuid.UUID) -> list[Job]:
        """Get active generation jobs (queued or running) for a node."""
        stmt = (
            select(Job)
            .where(
                Job.course_node_id == node_id,
                Job.status.in_(["queued", "active"]),
                Job.job_type == "generate_structure",
            )
            .order_by(Job.queued_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_generation_jobs_in_tree(
        self, node_ids: list[uuid.UUID]
    ) -> list[Job]:
        """Get active generation jobs targeting any node in the tree."""
        if not node_ids:
            return []
        stmt = (
            select(Job)
            .where(
                Job.course_node_id.in_(node_ids),
                Job.status.in_(["queued", "active"]),
                Job.job_type == "generate_structure",
            )
            .order_by(Job.queued_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def propagate_failure(
        self, failed_job_id: uuid.UUID, *, error_message: str | None = None
    ) -> list[uuid.UUID]:
        """Propagate failure to all dependent jobs recursively.

        Finds jobs whose ``depends_on`` JSONB array contains the given
        job ID, marks them as failed, and recurses into their dependents.

        Args:
            failed_job_id: UUID of the job that failed.
            error_message: Override message. Defaults to
                ``"Dependency <uuid> failed"``.

        Returns:
            List of newly failed job IDs (may be empty).
        """
        msg = error_message or f"Dependency {failed_job_id} failed"

        queue: deque[uuid.UUID] = deque([failed_job_id])
        seen: set[uuid.UUID] = set()
        failed_ids: list[uuid.UUID] = []

        while queue:
            current_id = queue.popleft()
            dependents = await self._find_dependents(current_id)
            for job in dependents:
                if job.id in seen:
                    continue
                seen.add(job.id)
                if job.status in ("queued", "active"):
                    job.status = "failed"
                    job.error_message = msg
                    job.completed_at = datetime.now(UTC)
                    failed_ids.append(job.id)
                    queue.append(job.id)

        if failed_ids:
            await self._session.flush()
        return failed_ids

    async def _find_dependents(self, job_id: uuid.UUID) -> list[Job]:
        """Find all jobs whose depends_on contains the given job_id."""
        stmt = select(Job).where(Job.depends_on.contains([str(job_id)]))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_pending(self) -> int:
        """Count all queued jobs (for queue estimates)."""
        from sqlalchemy import func

        stmt = select(func.count()).select_from(Job).where(Job.status == "queued")
        result = await self._session.execute(stmt)
        return result.scalar_one()
