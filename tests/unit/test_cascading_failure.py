"""Tests for cascading job failure propagation (S3-019)."""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import Job


def _make_job(
    *,
    status: str = "queued",
    depends_on: list[str] | None = None,
) -> Job:
    """Create a mock Job with realistic attributes."""
    job = MagicMock(spec=Job)
    job.id = uuid.uuid4()
    job.status = status
    job.depends_on = depends_on
    job.error_message = None
    job.completed_at = None
    return job


class TestPropagateFailure:
    """Tests for JobRepository.propagate_failure()."""

    @pytest.fixture()
    def session(self) -> AsyncMock:
        s = AsyncMock()
        s.flush = AsyncMock()
        return s

    @pytest.fixture()
    def repo(self, session: AsyncMock) -> JobRepository:
        return JobRepository(session)

    @pytest.mark.asyncio
    async def test_single_level(self, repo: JobRepository) -> None:
        """A depends on B. B fails -> A fails."""
        job_b = _make_job(status="failed")
        job_a = _make_job(status="queued", depends_on=[str(job_b.id)])

        async def find_dependents(job_id: uuid.UUID) -> list[Job]:
            if job_id == job_b.id:
                return [job_a]
            return []

        repo._find_dependents = AsyncMock(side_effect=find_dependents)  # type: ignore[method-assign]

        failed = await repo.propagate_failure(job_b.id)

        assert len(failed) == 1
        assert failed[0] == job_a.id
        assert job_a.status == "failed"
        assert "failed" in job_a.error_message
        assert isinstance(job_a.completed_at, datetime)

    @pytest.mark.asyncio
    async def test_multi_level(self, repo: JobRepository) -> None:
        """A -> B -> C. C fails -> B fails -> A fails."""
        job_c = _make_job(status="failed")
        job_b = _make_job(status="queued", depends_on=[str(job_c.id)])
        job_a = _make_job(status="queued", depends_on=[str(job_b.id)])

        async def find_dependents(job_id: uuid.UUID) -> list[Job]:
            if job_id == job_c.id:
                return [job_b]
            if job_id == job_b.id:
                return [job_a]
            return []

        repo._find_dependents = AsyncMock(side_effect=find_dependents)  # type: ignore[method-assign]

        failed = await repo.propagate_failure(job_c.id)

        assert len(failed) == 2
        assert job_b.id in failed
        assert job_a.id in failed
        assert job_b.status == "failed"
        assert job_a.status == "failed"

    @pytest.mark.asyncio
    async def test_diamond_dependency(self, repo: JobRepository) -> None:
        """A -> [B, C]. B -> D. C -> D. D fails -> B,C fail -> A fails."""
        job_d = _make_job(status="failed")
        job_b = _make_job(status="queued", depends_on=[str(job_d.id)])
        job_c = _make_job(status="queued", depends_on=[str(job_d.id)])
        job_a = _make_job(status="queued", depends_on=[str(job_b.id), str(job_c.id)])

        async def find_dependents(job_id: uuid.UUID) -> list[Job]:
            if job_id == job_d.id:
                return [job_b, job_c]
            if job_id == job_b.id:
                return [job_a]
            if job_id == job_c.id:
                # A already failed via B path
                return [job_a]
            return []

        repo._find_dependents = AsyncMock(side_effect=find_dependents)  # type: ignore[method-assign]

        failed = await repo.propagate_failure(job_d.id)

        # A appears only once (already failed when C path reaches it)
        assert job_b.id in failed
        assert job_c.id in failed
        assert job_a.id in failed
        assert job_a.status == "failed"

    @pytest.mark.asyncio
    async def test_already_completed_not_affected(self, repo: JobRepository) -> None:
        """Completed jobs are not affected by failure propagation."""
        job_b = _make_job(status="failed")
        job_a = _make_job(status="complete", depends_on=[str(job_b.id)])

        async def find_dependents(job_id: uuid.UUID) -> list[Job]:
            if job_id == job_b.id:
                return [job_a]
            return []

        repo._find_dependents = AsyncMock(side_effect=find_dependents)  # type: ignore[method-assign]

        failed = await repo.propagate_failure(job_b.id)

        assert len(failed) == 0
        assert job_a.status == "complete"

    @pytest.mark.asyncio
    async def test_idempotent_on_already_failed(self, repo: JobRepository) -> None:
        """Propagating failure on already-failed job produces no changes."""
        job_b = _make_job(status="failed")
        job_a = _make_job(status="failed", depends_on=[str(job_b.id)])

        async def find_dependents(job_id: uuid.UUID) -> list[Job]:
            if job_id == job_b.id:
                return [job_a]
            return []

        repo._find_dependents = AsyncMock(side_effect=find_dependents)  # type: ignore[method-assign]

        failed = await repo.propagate_failure(job_b.id)

        assert len(failed) == 0

    @pytest.mark.asyncio
    async def test_no_dependents(self, repo: JobRepository) -> None:
        """No dependents -> empty list returned."""
        job = _make_job(status="failed")
        repo._find_dependents = AsyncMock(return_value=[])  # type: ignore[method-assign]

        failed = await repo.propagate_failure(job.id)

        assert failed == []

    @pytest.mark.asyncio
    async def test_error_message_references_failed_job(
        self, repo: JobRepository
    ) -> None:
        """Error message includes the failed dependency UUID."""
        job_b = _make_job(status="failed")
        job_a = _make_job(status="queued", depends_on=[str(job_b.id)])

        repo._find_dependents = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda jid: [job_a] if jid == job_b.id else []
        )

        await repo.propagate_failure(job_b.id)

        assert str(job_b.id) in job_a.error_message

    @pytest.mark.asyncio
    async def test_active_job_also_failed(self, repo: JobRepository) -> None:
        """Active (running) jobs are also failed on dependency failure."""
        job_b = _make_job(status="failed")
        job_a = _make_job(status="active", depends_on=[str(job_b.id)])

        repo._find_dependents = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda jid: [job_a] if jid == job_b.id else []
        )

        failed = await repo.propagate_failure(job_b.id)

        assert len(failed) == 1
        assert job_a.status == "failed"
