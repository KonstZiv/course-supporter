"""Tests for material upload validation edge cases.

Covers file extension validation per source_type that is NOT duplicated
in ``test_material_entries.py`` (which tests CRUD + tenant isolation).
"""

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
from course_supporter.storage.material_entry_repository import MaterialEntryRepository
from course_supporter.storage.material_node_repository import MaterialNodeRepository

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)

ENQUEUE_FUNC = "course_supporter.api.routes.materials.enqueue_ingestion"


def _mock_node(
    *,
    node_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock node that passes tenant isolation."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.tenant_id = tenant_id or STUB_TENANT.tenant_id
    return node


def _mock_entry(
    *,
    node_id: uuid.UUID | None = None,
    source_type: str = "text",
    source_url: str = "https://example.com/doc.md",
    filename: str | None = None,
    state: str = "raw",
) -> MagicMock:
    """Create a mock MaterialEntry."""
    entry = MagicMock()
    entry.id = uuid.uuid4()
    entry.node_id = node_id or uuid.uuid4()
    entry.source_type = source_type
    entry.source_url = source_url
    entry.filename = filename
    entry.order = 0
    entry.state = state
    entry.error_message = None
    entry.pending_job_id = None
    entry.job_id = None
    entry.created_at = datetime.now(UTC)
    entry.updated_at = datetime.now(UTC)
    return entry


def _mock_job() -> MagicMock:
    """Create a mock Job returned by enqueue_ingestion."""
    job = MagicMock()
    job.id = uuid.uuid4()
    return job


@pytest.fixture()
def node_id() -> uuid.UUID:
    return uuid.uuid4()


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


class TestMaterialUploadValidation:
    """File extension validation edge cases for POST /nodes/{nid}/materials."""

    async def test_video_rejects_pdf_file(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """POST /materials rejects .pdf file for source_type 'video'."""
        response = await client.post(
            f"/api/v1/nodes/{node_id}/materials",
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
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """POST /materials rejects .mp4 file for source_type 'presentation'."""
        response = await client.post(
            f"/api/v1/nodes/{node_id}/materials",
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

    async def test_text_accepts_docx(
        self, client: AsyncClient, node_id: uuid.UUID, mock_s3: AsyncMock
    ) -> None:
        """POST /materials accepts .docx for source_type 'text'."""
        entry = _mock_entry(
            node_id=node_id,
            source_type="text",
            source_url="http://localhost:9000/key/notes.docx",
            filename="notes.docx",
        )
        job = _mock_job()
        with (
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(MaterialEntryRepository, "create", return_value=entry),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            response = await client.post(
                f"/api/v1/nodes/{node_id}/materials",
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
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """POST /materials rejects file without extension."""
        response = await client.post(
            f"/api/v1/nodes/{node_id}/materials",
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

    async def test_create_material_returns_state(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Created material includes state in response."""
        entry = _mock_entry(node_id=node_id, state="raw")
        job = _mock_job()
        with (
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(MaterialEntryRepository, "create", return_value=entry),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            response = await client.post(
                f"/api/v1/nodes/{node_id}/materials",
                data={
                    "source_type": "web",
                    "source_url": "https://example.com",
                },
            )
        assert response.status_code == 201
        assert response.json()["state"] == "raw"
