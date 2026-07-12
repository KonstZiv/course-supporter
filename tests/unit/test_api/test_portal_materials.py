"""Unit tests for the portal media route (Phase 6 T3, KD17).

``get_current_student`` is overridden to a fixed StudentContext; the gate
(material / tenant / enrollment) and S3 presigning are mocked. The live
presigned-GET (real MinIO 206) + real WebP addressing are covered by the
integration / live acceptance.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_current_student,
    get_s3_client,
    get_session,
)
from course_supporter.auth.context import StudentContext
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)

STUB_TENANT_ID = uuid.uuid4()
STUB_STUDENT_ID = uuid.uuid4()
STUB_STUDENT = StudentContext(
    student_id=STUB_STUDENT_ID,
    tenant_id=STUB_TENANT_ID,
    login="alice",
    display_name="Alice",
)


def _mock_material(
    *,
    source_type: str,
    source_url: str = "https://example.com/x",
    slide_keys: list[str] | None = None,
    course_root_id: uuid.UUID | None = None,
    deleted_at: object | None = None,
) -> MagicMock:
    material = MagicMock()
    material.source_type = source_type
    material.source_url = source_url
    material.slide_keys = slide_keys
    material.course_root_id = course_root_id or uuid.uuid4()
    material.deleted_at = deleted_at
    return material


def _mock_root_node(tenant_id: uuid.UUID) -> MagicMock:
    node = MagicMock()
    node.id = uuid.uuid4()
    node.tenant_id = tenant_id
    return node


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.generate_presigned_get_url = AsyncMock(
        side_effect=lambda key, **_: f"https://signed/{key}"
    )
    s3.extract_key = MagicMock(return_value=None)
    return s3


@pytest.fixture()
async def client(mock_session: AsyncMock, mock_s3: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_student] = lambda: STUB_STUDENT
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


def _url(material_id: uuid.UUID) -> str:
    return f"/api/v1/portal/materials/{material_id}"


def _enrolled_material(material: MagicMock) -> tuple[Any, Any, Any]:
    """Context patching the gate to resolve `material` for an enrolled student."""
    return (
        patch.object(AuthoredDocumentRepository, "get_by_id", return_value=material),
        patch.object(
            CourseNodeRepository,
            "get_by_id",
            return_value=_mock_root_node(STUB_TENANT_ID),
        ),
        patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
    )


class TestPortalMediaDescriptor:
    async def test_external_returns_link(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """YouTube/web (non-S3 source_url) → kind=external + the link, no S3."""
        material = _mock_material(
            source_type="web", source_url="https://youtu.be/abc123"
        )
        mock_s3.extract_key = MagicMock(return_value=None)
        with (
            patch.object(
                AuthoredDocumentRepository, "get_by_id", return_value=material
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(STUB_TENANT_ID),
            ),
            patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
        ):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "kind": "external",
            "url": "https://youtu.be/abc123",
            "slide_urls": None,
        }
        mock_s3.generate_presigned_get_url.assert_not_called()

    async def test_file_returns_presigned(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """Uploaded media (S3 source_url) → kind=file + a presigned-GET URL."""
        material = _mock_material(
            source_type="video",
            source_url="http://localhost:9000/bucket/tenants/t/v.mp4",
        )
        mock_s3.extract_key = MagicMock(return_value="tenants/t/v.mp4")
        get_by_id, node_by_id, enrolled = _enrolled_material(material)
        with get_by_id, node_by_id, enrolled:
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "file"
        assert body["url"] == "https://signed/tenants/t/v.mp4"
        assert body["slide_urls"] is None

    async def test_code_returns_attachment_disposition(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """R3/R7 (task-code-materials): code media is signed download-only."""
        material = _mock_material(
            source_type="code",
            source_url="http://localhost:9000/bucket/tenants/t/script.py",
        )
        material.filename = "script.py"
        mock_s3.extract_key = MagicMock(return_value="tenants/t/script.py")
        get_by_id, node_by_id, enrolled = _enrolled_material(material)
        with get_by_id, node_by_id, enrolled:
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "file"
        call = mock_s3.generate_presigned_get_url.await_args
        assert call.kwargs["content_disposition"] == (
            'attachment; filename="script.py"'
        )

    async def test_presentation_returns_slide_urls(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """Presentation → kind=slides + ordered presigned-GET URLs per WebP."""
        material = _mock_material(
            source_type="presentation",
            slide_keys=["t/n/slides/d/0001.webp", "t/n/slides/d/0002.webp"],
        )
        get_by_id, node_by_id, enrolled = _enrolled_material(material)
        with get_by_id, node_by_id, enrolled:
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "slides"
        assert body["slide_urls"] == [
            "https://signed/t/n/slides/d/0001.webp",
            "https://signed/t/n/slides/d/0002.webp",
        ]
        assert body["url"] is None

    async def test_presentation_pre_t3_empty_slides_no_500(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """A presentation ingested before T3 (slide_keys NULL) → empty list."""
        material = _mock_material(source_type="presentation", slide_keys=None)
        get_by_id, node_by_id, enrolled = _enrolled_material(material)
        with get_by_id, node_by_id, enrolled:
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"kind": "slides", "url": None, "slide_urls": []}
        mock_s3.generate_presigned_get_url.assert_not_called()


class TestPortalMediaGate:
    async def test_unknown_material_returns_404(self, client: AsyncClient) -> None:
        """Missing material → generic 404."""
        with patch.object(AuthoredDocumentRepository, "get_by_id", return_value=None):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Material not found."

    async def test_soft_deleted_returns_404(self, client: AsyncClient) -> None:
        """Soft-deleted material → generic 404."""
        from datetime import UTC, datetime

        material = _mock_material(source_type="video", deleted_at=datetime.now(UTC))
        with patch.object(
            AuthoredDocumentRepository, "get_by_id", return_value=material
        ):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 404

    async def test_foreign_tenant_returns_404(self, client: AsyncClient) -> None:
        """Material in another tenant → generic 404 (no existence leak)."""
        material = _mock_material(source_type="video")
        with (
            patch.object(
                AuthoredDocumentRepository, "get_by_id", return_value=material
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(uuid.uuid4()),  # other tenant
            ),
        ):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 404

    async def test_not_enrolled_returns_404(self, client: AsyncClient) -> None:
        """Not enrolled in the material's course → generic 404."""
        material = _mock_material(source_type="video")
        with (
            patch.object(
                AuthoredDocumentRepository, "get_by_id", return_value=material
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(STUB_TENANT_ID),
            ),
            patch.object(
                StudentEnrollmentRepository, "is_enrolled", return_value=False
            ),
        ):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 404
