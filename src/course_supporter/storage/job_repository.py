"""Repository for Job CRUD and status management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import Job, MaterialNode

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
    through ``MaterialNode.tenant_id``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        tenant_id: uuid.UUID | None = None,
        materialnode_id: uuid.UUID | None = None,
        job_type: str,
        priority: str = "normal",
        arq_job_id: str | None = None,
        input_params: dict[str, object] | None = None,
        depends_on: list[str] | None = None,
        estimated_at: datetime | None = None,
    ) -> Job:
        """Create a new job record."""
        job = Job(
            tenant_id=tenant_id,
            materialnode_id=materialnode_id,
            job_type=job_type,
            priority=priority,
            arq_job_id=arq_job_id,
            input_params=input_params,
            depends_on=depends_on,
            estimated_at=estimated_at,
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

    async def get_by_id_for_tenant(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Job | None:
        """Get a job by ID, ensuring it belongs to the given tenant.

        Joins through ``job.materialnode_id → material_node.tenant_id`` for isolation.
        Falls back to ``job.tenant_id`` for jobs without a linked node.
        """
        # Try node-based isolation first
        stmt = (
            select(Job)
            .join(MaterialNode, Job.materialnode_id == MaterialNode.id)
            .where(Job.id == job_id, MaterialNode.tenant_id == tenant_id)
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
            .where(Job.materialnode_id == node_id, Job.status.in_(["queued", "active"]))
            .order_by(Job.queued_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_generation_jobs(self, node_id: uuid.UUID) -> list[Job]:
        """Get active generation jobs (queued or running) for a node."""
        stmt = (
            select(Job)
            .where(
                Job.materialnode_id == node_id,
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
                Job.materialnode_id.in_(node_ids),
                Job.status.in_(["queued", "active"]),
                Job.job_type == "generate_structure",
            )
            .order_by(Job.queued_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_pending(self) -> int:
        """Count all queued jobs (for queue estimates)."""
        from sqlalchemy import func

        stmt = select(func.count()).select_from(Job).where(Job.status == "queued")
        result = await self._session.execute(stmt)
        return result.scalar_one()
