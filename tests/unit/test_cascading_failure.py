"""Tests for cascading job failure propagation (S3-019).

``propagate_failure`` no longer writes ``Job.status`` itself — since L1a the
status owner is ``JobRepository.update_status`` (contract §3 "Ownership").
These tests therefore assert on the OWNER calls (which dependent, to which
status, with which error message) rather than on direct attribute mutation of
the dependent mocks. ``update_status`` is spied so the propagation logic —
which dependents are in-flight, the BFS order, dedup — is what is exercised.
"""

from __future__ import annotations

import uuid
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
        r = JobRepository(session)
        # Spy on the status owner: propagate_failure delegates the status
        # write, so the unit under test here is the propagation graph walk.
        r.update_status = AsyncMock()  # type: ignore[method-assign]
        return r

    @staticmethod
    def _failed_ids(repo: JobRepository) -> set[uuid.UUID]:
        return {c.args[0] for c in repo.update_status.await_args_list}  # type: ignore[attr-defined]

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

        assert failed == [job_a.id]
        call = repo.update_status.await_args  # type: ignore[attr-defined]
        assert call.args == (job_a.id, "failed")
        assert "failed" in call.kwargs["error_message"]

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

        assert set(failed) == {job_b.id, job_a.id}
        assert self._failed_ids(repo) == {job_b.id, job_a.id}

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

        # A appears only once (already seen when the C path reaches it).
        assert set(failed) == {job_b.id, job_c.id, job_a.id}
        assert self._failed_ids(repo) == {job_b.id, job_c.id, job_a.id}
        assert repo.update_status.await_count == 3  # type: ignore[attr-defined]

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

        assert failed == []
        repo.update_status.assert_not_awaited()  # type: ignore[attr-defined]

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

        assert failed == []
        repo.update_status.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_no_dependents(self, repo: JobRepository) -> None:
        """No dependents -> empty list returned."""
        job = _make_job(status="failed")
        repo._find_dependents = AsyncMock(return_value=[])  # type: ignore[method-assign]

        failed = await repo.propagate_failure(job.id)

        assert failed == []
        repo.update_status.assert_not_awaited()  # type: ignore[attr-defined]

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

        call = repo.update_status.await_args  # type: ignore[attr-defined]
        assert str(job_b.id) in call.kwargs["error_message"]

    @pytest.mark.asyncio
    async def test_active_job_also_failed(self, repo: JobRepository) -> None:
        """Active jobs are also failed on dependency failure."""
        job_b = _make_job(status="failed")
        job_a = _make_job(status="active", depends_on=[str(job_b.id)])

        repo._find_dependents = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda jid: [job_a] if jid == job_b.id else []
        )

        failed = await repo.propagate_failure(job_b.id)

        assert failed == [job_a.id]
        call = repo.update_status.await_args  # type: ignore[attr-defined]
        assert call.args == (job_a.id, "failed")
