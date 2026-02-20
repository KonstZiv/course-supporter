"""Tests for POST /courses/{id}/materials endpoint."""

import io
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.repositories import (
    CourseRepository,
    SourceMaterialRepository,
)

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)

ENQUEUE_FUNC = "course_supporter.api.routes.courses.enqueue_ingestion"


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


def _make_job_mock() -> MagicMock:
    """Create a mock Job ORM object."""
    j = MagicMock()
    j.id = uuid.uuid4()
    return j


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
    s3.upload_smart = AsyncMock(
        return_value=("http://localhost:9000/course-materials/key/file.pdf", 11)
    )
    return s3


@pytest.fixture()
def mock_arq_redis() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
async def client(
    mock_session: AsyncMock, mock_s3: AsyncMock, mock_arq_redis: AsyncMock
) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_arq_redis] = lambda: mock_arq_redis
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestCreateMaterialAPI:
    async def test_create_material_with_url_returns_201(
        self, client: AsyncClient
    ) -> None:
        """POST /materials with source_url returns 201."""
        course_id = uuid.uuid4()
        material = _make_material_mock()
        job = _make_job_mock()
        with (
            patch.object(CourseRepository, "get_by_id", return_value=MagicMock()),
            patch.object(SourceMaterialRepository, "create", return_value=material),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
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
        assert data["job_id"] == str(job.id)

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
        job = _make_job_mock()
        with (
            patch.object(CourseRepository, "get_by_id", return_value=MagicMock()),
            patch.object(SourceMaterialRepository, "create", return_value=material),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
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
        mock_s3.upload_smart.assert_awaited_once()
        assert response.json()["job_id"] == str(job.id)

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

    async def test_create_material_no_url_no_file(self, client: AsyncClient) -> None:
        """POST /materials rejects when neither URL nor file provided."""
        response = await client.post(
            f"/api/v1/courses/{uuid.uuid4()}/materials",
            data={"source_type": "web"},
        )
        assert response.status_code == 422

    async def test_create_material_returns_pending_status(
        self, client: AsyncClient
    ) -> None:
        """Created material starts with 'pending' status."""
        material = _make_material_mock(status="pending")
        job = _make_job_mock()
        with (
            patch.object(CourseRepository, "get_by_id", return_value=MagicMock()),
            patch.object(
                SourceMaterialRepository,
                "create",
                return_value=material,
            ),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            response = await client.post(
                f"/api/v1/courses/{uuid.uuid4()}/materials",
                data={
                    "source_type": "web",
                    "source_url": "https://example.com",
                },
            )
        assert response.json()["status"] == "pending"

    async def test_web_source_type_rejects_file(self, client: AsyncClient) -> None:
        """POST /materials rejects file upload for source_type 'web'."""
        response = await client.post(
            f"/api/v1/courses/{uuid.uuid4()}/materials",
            data={"source_type": "web"},
            files={
                "file": ("page.html", io.BytesIO(b"<html>"), "text/html"),
            },
        )
        assert response.status_code == 422
        assert "does not accept file uploads" in response.json()["detail"]

    async def test_video_rejects_pdf_file(self, client: AsyncClient) -> None:
        """POST /materials rejects .pdf file for source_type 'video'."""
        response = await client.post(
            f"/api/v1/courses/{uuid.uuid4()}/materials",
            data={"source_type": "video"},
            files={
                "file": (
                    "slides.pdf",
                    io.BytesIO(b"PDF content"),
                    "application/pdf",
                ),
            },
        )
        assert response.status_code == 422
        assert "'.pdf' is not allowed" in response.json()["detail"]
        assert "'.mp4'" in response.json()["detail"]

    async def test_presentation_rejects_mp4_file(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /materials rejects .mp4 file for source_type 'presentation'."""
        response = await client.post(
            f"/api/v1/courses/{uuid.uuid4()}/materials",
            data={"source_type": "presentation"},
            files={
                "file": (
                    "video.mp4",
                    io.BytesIO(b"video data"),
                    "video/mp4",
                ),
            },
        )
        assert response.status_code == 422
        assert "'.mp4' is not allowed" in response.json()["detail"]

    async def test_text_accepts_docx(self, client: AsyncClient) -> None:
        """POST /materials accepts .docx for source_type 'text'."""
        material = _make_material_mock(
            source_type="text",
            source_url="http://localhost:9000/key/notes.docx",
            filename="notes.docx",
        )
        job = _make_job_mock()
        with (
            patch.object(
                CourseRepository,
                "get_by_id",
                return_value=MagicMock(),
            ),
            patch.object(
                SourceMaterialRepository,
                "create",
                return_value=material,
            ),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            response = await client.post(
                f"/api/v1/courses/{uuid.uuid4()}/materials",
                data={"source_type": "text"},
                files={
                    "file": (
                        "notes.docx",
                        io.BytesIO(b"docx data"),
                        "application/vnd.openxmlformats",
                    ),
                },
            )
        assert response.status_code == 201

    async def test_file_without_extension_rejected(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /materials rejects file without extension."""
        response = await client.post(
            f"/api/v1/courses/{uuid.uuid4()}/materials",
            data={"source_type": "video"},
            files={
                "file": (
                    "videofile",
                    io.BytesIO(b"data"),
                    "application/octet-stream",
                ),
            },
        )
        assert response.status_code == 422
