"""Tests for material entry API endpoints (tree-based materials)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.material_entry_repository import MaterialEntryRepository
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.repositories import CourseRepository

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)

ENQUEUE_FUNC = "course_supporter.api.routes.materials.enqueue_ingestion"


def _mock_course(course_id: uuid.UUID) -> MagicMock:
    """Create a mock Course that passes tenant isolation."""
    course = MagicMock()
    course.id = course_id
    course.tenant_id = STUB_TENANT.tenant_id
    return course


def _mock_node(
    *,
    node_id: uuid.UUID | None = None,
    course_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock MaterialNode."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.course_id = course_id or uuid.uuid4()
    return node


def _mock_entry(
    *,
    entry_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    source_type: str = "text",
    source_url: str = "https://example.com/doc.md",
    filename: str | None = None,
    order: int = 0,
    state: str = "raw",
    error_message: str | None = None,
    pending_job_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock MaterialEntry with ORM-compatible attributes."""
    entry = MagicMock()
    entry.id = entry_id or uuid.uuid4()
    entry.node_id = node_id or uuid.uuid4()
    entry.source_type = source_type
    entry.source_url = source_url
    entry.filename = filename
    entry.order = order
    entry.state = state
    entry.error_message = error_message
    entry.pending_job_id = pending_job_id
    entry.job_id = None  # Not an ORM field; prevent MagicMock auto-creation
    entry.created_at = datetime.now(UTC)
    entry.updated_at = datetime.now(UTC)
    return entry


def _mock_job(job_id: uuid.UUID | None = None) -> MagicMock:
    """Create a mock Job returned by enqueue_ingestion."""
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    return job


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
def mock_arq() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def course_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def node_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
async def client(mock_session: AsyncMock, mock_arq: MagicMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_arq_redis] = lambda: mock_arq
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestCreateMaterial:
    """POST /api/v1/courses/{id}/nodes/{nid}/materials"""

    async def test_returns_201(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Successful material creation returns 201 with job_id."""
        entry = _mock_entry(node_id=node_id)
        job = _mock_job()
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=course_id),
            ),
            patch.object(MaterialEntryRepository, "create", return_value=entry),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/materials",
                json={
                    "source_type": "text",
                    "source_url": "https://example.com/doc.md",
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == str(entry.id)
        assert data["job_id"] == str(job.id)
        assert data["source_type"] == "text"

    async def test_invalid_source_type_returns_422(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Invalid source_type is rejected by Pydantic validation."""
        resp = await client.post(
            f"/api/v1/courses/{course_id}/nodes/{node_id}/materials",
            json={
                "source_type": "invalid",
                "source_url": "https://example.com/doc.md",
            },
        )
        assert resp.status_code == 422

    async def test_course_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Non-existent course returns 404."""
        with patch.object(CourseRepository, "get_by_id", return_value=None):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/materials",
                json={
                    "source_type": "text",
                    "source_url": "https://example.com/doc.md",
                },
            )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Course not found"

    async def test_node_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Non-existent node returns 404."""
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=None),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/materials",
                json={
                    "source_type": "text",
                    "source_url": "https://example.com/doc.md",
                },
            )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Node not found"

    async def test_node_wrong_course_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Node belonging to another course returns 404."""
        other_course = uuid.uuid4()
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=other_course),
            ),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/materials",
                json={
                    "source_type": "text",
                    "source_url": "https://example.com/doc.md",
                },
            )
        assert resp.status_code == 404

    async def test_with_filename(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Creation with filename includes it in response."""
        entry = _mock_entry(node_id=node_id, filename="notes.md")
        job = _mock_job()
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=course_id),
            ),
            patch.object(MaterialEntryRepository, "create", return_value=entry),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/materials",
                json={
                    "source_type": "text",
                    "source_url": "https://example.com/notes.md",
                    "filename": "notes.md",
                },
            )
        assert resp.status_code == 201
        assert resp.json()["filename"] == "notes.md"


class TestListMaterials:
    """GET /api/v1/courses/{id}/nodes/{nid}/materials"""

    async def test_returns_list(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Returns list of materials for the node."""
        entries = [
            _mock_entry(node_id=node_id, order=0),
            _mock_entry(node_id=node_id, order=1, source_type="video"),
        ]
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=course_id),
            ),
            patch.object(MaterialEntryRepository, "get_for_node", return_value=entries),
        ):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/materials",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["order"] == 0
        assert data[1]["source_type"] == "video"

    async def test_empty_list(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Returns empty list when node has no materials."""
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=course_id),
            ),
            patch.object(MaterialEntryRepository, "get_for_node", return_value=[]),
        ):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/materials",
            )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_course_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Non-existent course returns 404."""
        with patch.object(CourseRepository, "get_by_id", return_value=None):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/materials",
            )
        assert resp.status_code == 404


class TestGetMaterial:
    """GET /api/v1/courses/{id}/materials/{mid}"""

    async def test_returns_entry(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Returns single material entry."""
        entry = _mock_entry(node_id=node_id, state="ready")
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialEntryRepository, "get_by_id", return_value=entry),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=course_id),
            ),
        ):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/materials/{entry.id}",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(entry.id)
        assert data["state"] == "ready"

    async def test_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent material returns 404."""
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialEntryRepository, "get_by_id", return_value=None),
        ):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/materials/{uuid.uuid4()}",
            )
        assert resp.status_code == 404

    async def test_wrong_course_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Material belonging to another course returns 404."""
        entry = _mock_entry(node_id=node_id)
        other_course = uuid.uuid4()
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialEntryRepository, "get_by_id", return_value=entry),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=other_course),
            ),
        ):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/materials/{entry.id}",
            )
        assert resp.status_code == 404


class TestDeleteMaterial:
    """DELETE /api/v1/courses/{id}/materials/{mid}"""

    async def test_returns_204(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Successful deletion returns 204."""
        entry = _mock_entry(node_id=node_id)
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialEntryRepository, "get_by_id", return_value=entry),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=course_id),
            ),
            patch.object(MaterialEntryRepository, "delete", return_value=None),
        ):
            resp = await client.delete(
                f"/api/v1/courses/{course_id}/materials/{entry.id}",
            )
        assert resp.status_code == 204

    async def test_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent material returns 404."""
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialEntryRepository, "get_by_id", return_value=None),
        ):
            resp = await client.delete(
                f"/api/v1/courses/{course_id}/materials/{uuid.uuid4()}",
            )
        assert resp.status_code == 404

    async def test_course_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent course returns 404."""
        with patch.object(CourseRepository, "get_by_id", return_value=None):
            resp = await client.delete(
                f"/api/v1/courses/{course_id}/materials/{uuid.uuid4()}",
            )
        assert resp.status_code == 404


class TestRetryMaterial:
    """POST /api/v1/courses/{id}/materials/{mid}/retry"""

    async def test_returns_200_with_new_job(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Successful retry returns 200 with new job_id."""
        entry = _mock_entry(
            node_id=node_id,
            state="error",
            error_message="Processing failed",
        )
        job = _mock_job()
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialEntryRepository, "get_by_id", return_value=entry),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=course_id),
            ),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/materials/{entry.id}/retry",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == str(job.id)

    async def test_non_error_state_returns_409(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Retry on non-error material returns 409."""
        entry = _mock_entry(node_id=node_id, state="ready")
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialEntryRepository, "get_by_id", return_value=entry),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=course_id),
            ),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/materials/{entry.id}/retry",
            )
        assert resp.status_code == 409
        assert "ready" in resp.json()["detail"]

    async def test_pending_state_returns_409(
        self, client: AsyncClient, course_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """Retry on pending material returns 409."""
        entry = _mock_entry(node_id=node_id, state="pending")
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialEntryRepository, "get_by_id", return_value=entry),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, course_id=course_id),
            ),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/materials/{entry.id}/retry",
            )
        assert resp.status_code == 409

    async def test_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent material returns 404."""
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialEntryRepository, "get_by_id", return_value=None),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/materials/{uuid.uuid4()}/retry",
            )
        assert resp.status_code == 404
