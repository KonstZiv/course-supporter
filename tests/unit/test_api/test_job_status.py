"""Tests for GET /api/v1/jobs/{job_id} and POST /api/v1/jobs/{job_id}/reactivate."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.job_repository import JobRepository

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    plan_id="basic",
    key_prefix="cs_test",
)


def _make_job_mock(
    *,
    job_id: uuid.UUID | None = None,
    job_type: str = "document_processing",
    priority: str = "normal",
    status: str = "queued",
    tenant_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    arq_job_id: str | None = "arq:test:123",
    error_message: str | None = None,
    queued_at: datetime | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> MagicMock:
    """Create a mock Job ORM object."""
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    job.job_type = job_type
    job.priority = priority
    job.status = status
    job.tenant_id = uuid.uuid4()
    job.course_id = None  # removed field
    job.course_node_id = node_id
    job.arq_job_id = arq_job_id
    job.current_stage = None
    job.stage_progress = None
    job.result_data = None
    job.error_message = error_message
    job.error_category = None
    job.queued_at = queued_at or datetime.now(UTC)
    job.started_at = started_at
    job.completed_at = completed_at
    return job


@pytest.fixture()
def mock_session() -> MagicMock:
    session = MagicMock()
    # commit() is awaited by the reactivate route at the end of the handler
    session.commit = AsyncMock()
    return session


@pytest.fixture()
def mock_arq() -> MagicMock:
    arq = MagicMock()
    enqueued = MagicMock()
    enqueued.job_id = "arq:new:reactivated"
    arq.enqueue_job = AsyncMock(return_value=enqueued)
    return arq


@pytest.fixture()
async def client(mock_session: MagicMock, mock_arq: MagicMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_arq_redis] = lambda: mock_arq
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
        assert data["job_type"] == "document_processing"
        assert data["priority"] == "normal"
        assert data["status"] == "active"
        assert data["tenant_id"] == str(job.tenant_id)
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


# ── POST /api/v1/jobs/{job_id}/reactivate ─────────────────────────


def _make_failed_ingest_job(
    *, node_id: uuid.UUID | None = None, job_type: str = "document_processing"
) -> MagicMock:
    """Failed Job with ``input_params`` sufficient for the ingest dispatcher.

    ``job_type`` defaults to ``"document_processing"`` (the happy-path
    dispatcher arm → ``arq_ingest_material``); pass another string to
    exercise the unsupported-type branch directly instead of patching the
    attribute on the returned mock.
    """
    job = _make_job_mock(
        status="failed",
        job_type=job_type,
        node_id=node_id or uuid.uuid4(),
        error_message="boom",
        completed_at=datetime.now(UTC),
    )
    job.input_params = {
        "material_id": str(uuid.uuid4()),
        "source_type": "web",
        "source_url": "https://example.com",
    }
    return job


class TestReactivateJobHappyPath:
    """POST /jobs/{id}/reactivate — success returns 200 + enqueues new ARQ task."""

    async def test_returns_200_for_failed_job(
        self, client: AsyncClient, mock_arq: MagicMock
    ) -> None:
        """Failed job → 200 with updated JobResponse + ARQ enqueue called once."""
        job = _make_failed_ingest_job()
        with (
            patch.object(JobRepository, "get_by_id_for_tenant", return_value=job),
            patch.object(JobRepository, "reactivate", return_value=job),
            patch.object(JobRepository, "set_arq_job_id", return_value=None),
        ):
            response = await client.post(f"/api/v1/jobs/{job.id}/reactivate")
        assert response.status_code == 200
        assert response.json()["id"] == str(job.id)
        # Exactly one ARQ task enqueued — no duplicate dispatch
        mock_arq.enqueue_job.assert_called_once()

    async def test_arq_called_with_resolved_function_and_args(
        self, client: AsyncClient, mock_arq: MagicMock
    ) -> None:
        """document_processing dispatcher calls ``arq_ingest_material`` (positional)."""
        job = _make_failed_ingest_job()
        with (
            patch.object(JobRepository, "get_by_id_for_tenant", return_value=job),
            patch.object(JobRepository, "reactivate", return_value=job),
            patch.object(JobRepository, "set_arq_job_id", return_value=None),
        ):
            await client.post(f"/api/v1/jobs/{job.id}/reactivate")

        call_args = mock_arq.enqueue_job.call_args
        assert call_args.args[0] == "arq_ingest_material"
        # First positional arg after function name is jid (str)
        assert call_args.args[1] == str(job.id)


class TestReactivateJobNotFound:
    """404 / 422 / 409 error branches."""

    async def test_missing_job_returns_404(self, client: AsyncClient) -> None:
        """Job not found (or tenant mismatch) → 404."""
        with patch.object(JobRepository, "get_by_id_for_tenant", return_value=None):
            response = await client.post(f"/api/v1/jobs/{uuid.uuid4()}/reactivate")
        assert response.status_code == 404
        assert response.json()["detail"] == "Job not found"

    async def test_invalid_uuid_returns_422(self, client: AsyncClient) -> None:
        """Invalid UUID in path → 422 from FastAPI's path parser."""
        response = await client.post("/api/v1/jobs/not-a-uuid/reactivate")
        assert response.status_code == 422

    async def test_non_failed_state_returns_409(self, client: AsyncClient) -> None:
        """``reactivate`` raises ValueError → 409 (wrong state)."""
        job = _make_failed_ingest_job()
        # Repo returns the job (so it's visible), but reactivate rejects it
        with (
            patch.object(JobRepository, "get_by_id_for_tenant", return_value=job),
            patch.object(
                JobRepository,
                "reactivate",
                side_effect=ValueError("Cannot reactivate Job ... in state 'queued'"),
            ),
        ):
            response = await client.post(f"/api/v1/jobs/{job.id}/reactivate")
        assert response.status_code == 409
        assert "Cannot reactivate" in response.json()["detail"]


class TestReactivateJobInputParamsMissing:
    """Dispatcher 422 — Job exists and is failed but input_params incomplete."""

    async def test_missing_required_input_param_returns_422(
        self, client: AsyncClient
    ) -> None:
        """Stripped input_params → dispatcher KeyError → HTTPException(422)."""
        job = _make_failed_ingest_job()
        # Strip a key the ingest dispatcher requires
        job.input_params = {"material_id": str(uuid.uuid4())}
        with (
            patch.object(JobRepository, "get_by_id_for_tenant", return_value=job),
            patch.object(JobRepository, "reactivate", return_value=job),
        ):
            response = await client.post(f"/api/v1/jobs/{job.id}/reactivate")
        assert response.status_code == 422
        assert "input_params missing" in response.json()["detail"]

    async def test_unsupported_job_type_returns_422(self, client: AsyncClient) -> None:
        """job_type outside the dispatcher's match set → 422 with supported list."""
        job = _make_failed_ingest_job(job_type="unknown_future_type")
        with (
            patch.object(JobRepository, "get_by_id_for_tenant", return_value=job),
            patch.object(JobRepository, "reactivate", return_value=job),
        ):
            response = await client.post(f"/api/v1/jobs/{job.id}/reactivate")
        assert response.status_code == 422
        assert "Reactivate not supported" in response.json()["detail"]
