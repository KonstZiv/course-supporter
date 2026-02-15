"""Tests for POST /courses/{id}/materials endpoint."""

import io
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.repositories import (
    CourseRepository,
    SourceMaterialRepository,
)

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["courses:write"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)

INGEST_TASK = "course_supporter.api.routes.courses.ingest_material"


def _make_material_mock(
    *,
    source_type: str = "web",
    source_url: str = "https://example.com/article",
    filename: str | None = None,
    status: str = "pending",
) -> MagicMock:
    """Create a mock SourceMaterial ORM object."""
    m = MagicMock()
    m.id = uuid.uuid4()
    m.source_type = source_type
    m.source_url = source_url
    m.filename = filename
    m.status = status
    m.created_at = datetime.now(UTC)
    return m


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.upload_file = AsyncMock(
        return_value="http://localhost:9000/course-materials/key/file.pdf"
    )
    return s3


@pytest.fixture()
async def client(mock_session: AsyncMock, mock_s3: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestCreateMaterialAPI:
    @pytest.mark.asyncio
    async def test_create_material_with_url_returns_201(
        self, client: AsyncClient
    ) -> None:
        """POST /materials with source_url returns 201."""
        course_id = uuid.uuid4()
        material = _make_material_mock()
        with (
            patch.object(CourseRepository, "get_by_id", return_value=MagicMock()),
            patch.object(SourceMaterialRepository, "create", return_value=material),
            patch(INGEST_TASK),
        ):
            response = await client.post(
                f"/api/v1/courses/{course_id}/materials",
                data={
                    "source_type": "web",
                    "source_url": "https://example.com/article",
                },
            )
        assert response.status_code == 201
        data = response.json()
        assert data["id"] == str(material.id)
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_create_material_with_file_returns_201(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """POST /materials with file upload returns 201."""
        course_id = uuid.uuid4()
        material = _make_material_mock(
            source_type="presentation",
            source_url="http://localhost:9000/key/file.pdf",
            filename="slides.pdf",
        )
        with (
            patch.object(CourseRepository, "get_by_id", return_value=MagicMock()),
            patch.object(SourceMaterialRepository, "create", return_value=material),
            patch(INGEST_TASK),
        ):
            response = await client.post(
                f"/api/v1/courses/{course_id}/materials",
                data={"source_type": "presentation"},
                files={
                    "file": (
                        "slides.pdf",
                        io.BytesIO(b"PDF content"),
                        "application/pdf",
                    )
                },
            )
        assert response.status_code == 201
        mock_s3.upload_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_material_course_not_found(self, client: AsyncClient) -> None:
        """POST /materials returns 404 for missing course."""
        with patch.object(CourseRepository, "get_by_id", return_value=None):
            response = await client.post(
                f"/api/v1/courses/{uuid.uuid4()}/materials",
                data={
                    "source_type": "web",
                    "source_url": "https://example.com",
                },
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_create_material_invalid_source_type(
        self, client: AsyncClient
    ) -> None:
        """POST /materials rejects invalid source_type."""
        response = await client.post(
            f"/api/v1/courses/{uuid.uuid4()}/materials",
            data={
                "source_type": "invalid",
                "source_url": "https://example.com",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_material_no_url_no_file(self, client: AsyncClient) -> None:
        """POST /materials rejects when neither URL nor file provided."""
        response = await client.post(
            f"/api/v1/courses/{uuid.uuid4()}/materials",
            data={"source_type": "web"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_material_returns_pending_status(
        self, client: AsyncClient
    ) -> None:
        """Created material starts with 'pending' status."""
        material = _make_material_mock(status="pending")
        with (
            patch.object(CourseRepository, "get_by_id", return_value=MagicMock()),
            patch.object(
                SourceMaterialRepository,
                "create",
                return_value=material,
            ),
            patch(INGEST_TASK),
        ):
            response = await client.post(
                f"/api/v1/courses/{uuid.uuid4()}/materials",
                data={
                    "source_type": "web",
                    "source_url": "https://example.com",
                },
            )
        assert response.json()["status"] == "pending"
