"""Tests for GET /api/v1/jobs/{job_id}."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.job_repository import JobRepository

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)


def _make_job_mock(
    *,
    job_id: uuid.UUID | None = None,
    job_type: str = "ingest",
    priority: str = "normal",
    status: str = "queued",
    course_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    arq_job_id: str | None = "arq:test:123",
    error_message: str | None = None,
    queued_at: datetime | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    estimated_at: datetime | None = None,
) -> MagicMock:
    """Create a mock Job ORM object."""
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    job.job_type = job_type
    job.priority = priority
    job.status = status
    job.course_id = course_id or uuid.uuid4()
    job.node_id = node_id
    job.arq_job_id = arq_job_id
    job.error_message = error_message
    job.queued_at = queued_at or datetime.now(UTC)
    job.started_at = started_at
    job.completed_at = completed_at
    job.estimated_at = estimated_at
    return job


@pytest.fixture()
def mock_session() -> MagicMock:
    return MagicMock()


@pytest.fixture()
async def client(mock_session: MagicMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestGetJob:
    """GET /api/v1/jobs/{job_id} — happy path."""

    async def test_returns_200_for_existing_job(self, client: AsyncClient) -> None:
        """Existing job returns 200."""
        job = _make_job_mock()
        with patch.object(JobRepository, "get_by_id_for_tenant", return_value=job):
            response = await client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 200

    async def test_response_contains_all_fields(self, client: AsyncClient) -> None:
        """Response JSON contains all JobResponse fields."""
        job = _make_job_mock(
            status="active",
            started_at=datetime.now(UTC),
        )
        with patch.object(JobRepository, "get_by_id_for_tenant", return_value=job):
            response = await client.get(f"/api/v1/jobs/{job.id}")
        data = response.json()
        assert data["id"] == str(job.id)
        assert data["job_type"] == "ingest"
        assert data["priority"] == "normal"
        assert data["status"] == "active"
        assert data["course_id"] == str(job.course_id)
        assert data["arq_job_id"] == "arq:test:123"
        assert data["started_at"] is not None
        assert data["error_message"] is None

    async def test_completed_job_has_timestamps(self, client: AsyncClient) -> None:
        """Completed job includes started_at and completed_at."""
        now = datetime.now(UTC)
        job = _make_job_mock(
            status="complete",
            started_at=now,
            completed_at=now,
        )
        with patch.object(JobRepository, "get_by_id_for_tenant", return_value=job):
            response = await client.get(f"/api/v1/jobs/{job.id}")
        data = response.json()
        assert data["started_at"] is not None
        assert data["completed_at"] is not None

    async def test_failed_job_has_error_message(self, client: AsyncClient) -> None:
        """Failed job includes error_message."""
        job = _make_job_mock(
            status="failed",
            error_message="Processing timeout",
            completed_at=datetime.now(UTC),
        )
        with patch.object(JobRepository, "get_by_id_for_tenant", return_value=job):
            response = await client.get(f"/api/v1/jobs/{job.id}")
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_message"] == "Processing timeout"

    async def test_queued_job_has_null_timestamps(self, client: AsyncClient) -> None:
        """Queued job has null started_at and completed_at."""
        job = _make_job_mock(status="queued")
        with patch.object(JobRepository, "get_by_id_for_tenant", return_value=job):
            response = await client.get(f"/api/v1/jobs/{job.id}")
        data = response.json()
        assert data["started_at"] is None
        assert data["completed_at"] is None


class TestGetJobNotFound:
    """GET /api/v1/jobs/{job_id} — 404 cases."""

    async def test_nonexistent_job_returns_404(self, client: AsyncClient) -> None:
        """Non-existent job returns 404."""
        with patch.object(JobRepository, "get_by_id_for_tenant", return_value=None):
            response = await client.get(f"/api/v1/jobs/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["detail"] == "Job not found"

    async def test_wrong_tenant_returns_404(self, client: AsyncClient) -> None:
        """Job belonging to another tenant returns 404.

        get_by_id_for_tenant filters by tenant_id, so it returns None.
        """
        with patch.object(JobRepository, "get_by_id_for_tenant", return_value=None):
            response = await client.get(f"/api/v1/jobs/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_invalid_uuid_returns_422(self, client: AsyncClient) -> None:
        """Invalid UUID in path returns 422."""
        response = await client.get("/api/v1/jobs/not-a-uuid")
        assert response.status_code == 422


class TestGetJobTenantIsolation:
    """Verify tenant_id is passed to repository."""

    async def test_passes_tenant_id_to_repo(self, client: AsyncClient) -> None:
        """Repository receives the correct tenant_id from auth context."""
        job = _make_job_mock()
        with patch.object(
            JobRepository, "get_by_id_for_tenant", return_value=job
        ) as mock_get:
            await client.get(f"/api/v1/jobs/{job.id}")
        mock_get.assert_called_once_with(job.id, STUB_TENANT.tenant_id)
