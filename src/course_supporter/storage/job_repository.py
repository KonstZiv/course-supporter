"""Repository for Job CRUD and status management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import Course, Job

# Valid job status transitions
JOB_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"active", "cancelled"},
    "active": {"complete", "failed"},
    "complete": set(),
    "failed": {"queued"},  # retry
    "cancelled": set(),
}


class JobRepository:
    """Repository for job tracking operations.

    Not tenant-scoped — jobs are accessed via course_id or directly by id.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        course_id: uuid.UUID | None = None,
        node_id: uuid.UUID | None = None,
        job_type: str,
        priority: str = "normal",
        arq_job_id: str | None = None,
        input_params: dict[str, object] | None = None,
        depends_on: list[str] | None = None,
        estimated_at: datetime | None = None,
    ) -> Job:
        """Create a new job record."""
        job = Job(
            course_id=course_id,
            node_id=node_id,
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
        result_material_id: uuid.UUID | None = None,
        result_snapshot_id: uuid.UUID | None = None,
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
            if result_material_id is not None and result_snapshot_id is not None:
                msg = "Cannot set both result_material_id and result_snapshot_id"
                raise ValueError(msg)
            if result_material_id is not None:
                values["result_material_id"] = result_material_id
            if result_snapshot_id is not None:
                values["result_snapshot_id"] = result_snapshot_id

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

        Joins through ``job.course_id → course.tenant_id`` for isolation.
        Jobs without a ``course_id`` are not accessible via this method.
        """
        stmt = (
            select(Job)
            .join(Course, Job.course_id == Course.id)
            .where(Job.id == job_id, Course.tenant_id == tenant_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_for_course(self, course_id: uuid.UUID) -> list[Job]:
        """Get all active (queued or running) jobs for a course."""
        stmt = (
            select(Job)
            .where(Job.course_id == course_id, Job.status.in_(["queued", "active"]))
            .order_by(Job.queued_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_for_node(self, node_id: uuid.UUID) -> list[Job]:
        """Get all active jobs for a specific node."""
        stmt = (
            select(Job)
            .where(Job.node_id == node_id, Job.status.in_(["queued", "active"]))
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

    async def get_for_course(
        self,
        course_id: uuid.UUID,
        *,
        status: str | None = None,
        job_type: str | None = None,
    ) -> list[Job]:
        """Get jobs for a course with optional filters."""
        stmt = select(Job).where(Job.course_id == course_id)
        if status is not None:
            stmt = stmt.where(Job.status == status)
        if job_type is not None:
            stmt = stmt.where(Job.job_type == job_type)
        stmt = stmt.order_by(Job.queued_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
