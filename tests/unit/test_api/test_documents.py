"""Tests for document upload validation edge cases.

Phase 1.2 C3 replaced hand-rolled ``ALLOWED_EXTENSIONS`` source-type
segmentation with canonical ``security/run_stage1`` against the flat
``AUTHORED_POLICY`` whitelist. Source-type-cross rejection (e.g.
".pdf" rejected for "video") is no longer a behavior — coverage of
the new wiring lives at the drift integration suite
(``tests/integration/test_authored_upload_validation.py``).

This module retains route-level KD14 wiring assertions for the
multipart ``POST /nodes/{nid}/documents`` and presigned
``POST /nodes/{nid}/documents/upload-url`` endpoints (status 400 +
KD14 detail schema). Magic-byte mismatch coverage lives at the
library tier (``tests/unit/security/test_stage1.py``).
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
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.database import get_session

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    plan_id="basic",
    key_prefix="cs_test",
)

ENQUEUE_FUNC = "course_supporter.api.routes.documents.enqueue_ingestion"


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
    task_type: str | None = None,
) -> MagicMock:
    """Create a mock AuthoredDocument."""
    entry = MagicMock()
    entry.id = uuid.uuid4()
    entry.course_node_id = node_id or uuid.uuid4()
    entry.source_type = source_type
    entry.material_role = "educational"
    entry.task_type = task_type
    entry.source_url = source_url
    entry.filename = filename
    entry.language = None
    entry.order = 0
    entry.state = state
    # L3: the response now carries a sibling ``processing_phase``. These tests
    # do not assert it — mirror the terminal state, default in-flight to
    # ``queued`` (the honest value for a freshly-enqueued create/confirm mock).
    entry.processing_phase = {"ready": "ready", "error": "error"}.get(state, "queued")
    entry.error_message = None
    entry.job_id = None
    entry.processing_estimate = None
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


RUN_STAGE1_AT_DOCUMENTS = "course_supporter.api.routes.documents.run_stage1"


class TestDocumentUploadValidation:
    """KD14 Stage 1 wiring at POST /nodes/{nid}/documents (multipart).

    Source-type-cross rejection (e.g. ".pdf" for "video") was removed
    in Phase 1.2 C3 — drift suite covers AUTHORED_POLICY whitelist.
    These tests verify route-level wiring of Stage 1 results to the
    KD14 HTTP envelope (200/201 on accept; 400 + KD14 detail on
    reject).
    """

    async def test_text_accepts_docx(
        self, client: AsyncClient, node_id: uuid.UUID, mock_s3: AsyncMock
    ) -> None:
        """POST /documents accepts .docx when Stage 1 returns ok."""
        entry = _mock_entry(
            node_id=node_id,
            source_type="text",
            source_url="http://localhost:9000/key/notes.docx",
            filename="notes.docx",
        )
        job = _mock_job()
        with (
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(AuthoredDocumentRepository, "create", return_value=entry),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
            # Bypass libmagic — fixture bytes are not a valid DOCX
            # archive. Phase 0.6 sealed-library tests cover magic
            # validation at the unit tier; here we exercise the
            # route-level happy-path wiring (Stage 1 succeeds → 201).
            patch(RUN_STAGE1_AT_DOCUMENTS, return_value=None),
        ):
            response = await client.post(
                f"/api/v1/nodes/{node_id}/documents",
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
        """POST /documents rejects file without extension via KD14."""
        response = await client.post(
            f"/api/v1/nodes/{node_id}/documents",
            data={"source_type": "video"},
            files={
                "file": (
                    "videofile",
                    io.BytesIO(b"data"),
                    "application/octet-stream",
                ),
            },
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "SECURITY_REJECTED"
        assert detail["category"] == "forbidden_type"
        assert "details" in detail

    async def test_create_document_returns_state(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Created document includes state in response."""
        entry = _mock_entry(node_id=node_id, state="raw")
        job = _mock_job()
        with (
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(AuthoredDocumentRepository, "create", return_value=entry),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            response = await client.post(
                f"/api/v1/nodes/{node_id}/documents",
                data={
                    "source_type": "web",
                    "source_url": "https://example.com",
                },
            )
        assert response.status_code == 201
        assert response.json()["state"] == "raw"

    async def test_create_document_accepts_task_type(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """POST /documents forwards task_type to the repository."""
        entry = _mock_entry(
            node_id=node_id,
            source_type="text",
            task_type="short_task",
        )
        job = _mock_job()
        create_mock = AsyncMock(return_value=entry)
        with (
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(AuthoredDocumentRepository, "create", create_mock),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            response = await client.post(
                f"/api/v1/nodes/{node_id}/documents",
                data={
                    "source_type": "text",
                    "source_url": "https://example.com/hw.md",
                    "task_type": "short_task",
                },
            )
        assert response.status_code == 201
        assert response.json()["task_type"] == "short_task"
        # Repository receives the enum value
        call_kwargs = create_mock.call_args.kwargs
        assert call_kwargs["task_type"] == "short_task"

    async def test_create_document_rejects_invalid_task_type(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """POST /documents rejects task_type outside the taxonomy."""
        response = await client.post(
            f"/api/v1/nodes/{node_id}/documents",
            data={
                "source_type": "text",
                "source_url": "https://example.com/x.md",
                "task_type": "essay",
            },
        )
        assert response.status_code == 422


# -- POST /nodes/{nid}/documents/upload-url --


_S3_PRESIGNED = "https://s3.example.com/bucket/key?sig=abc"


class TestGetUploadUrl:
    """Presigned URL generation for direct S3 upload."""

    async def test_200_returns_presigned_url(
        self, client: AsyncClient, node_id: uuid.UUID, mock_s3: AsyncMock
    ) -> None:
        """Returns presigned URL with key and expiry."""
        mock_s3.generate_presigned_url = AsyncMock(return_value=_S3_PRESIGNED)
        with patch.object(
            CourseNodeRepository,
            "get_by_id",
            return_value=_mock_node(node_id=node_id),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents/upload-url",
                json={
                    "filename": "slides.pdf",
                    "content_type": "application/pdf",
                    "source_type": "presentation",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["upload_url"] == _S3_PRESIGNED
        assert "tenants/" in data["key"]
        assert str(node_id) in data["key"]
        assert "slides.pdf" in data["key"]
        assert data["expires_in"] == 900

    async def test_422_web_source_type(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """source_type 'web' is rejected."""
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/documents/upload-url",
            json={
                "filename": "page.html",
                "content_type": "text/html",
                "source_type": "web",
            },
        )
        assert resp.status_code == 422

    async def test_code_extension_200(
        self, client: AsyncClient, node_id: uuid.UUID, mock_s3: AsyncMock
    ) -> None:
        """A code extension passes the presigned fast-gate for source_type=code."""
        mock_s3.generate_presigned_url = AsyncMock(return_value=_S3_PRESIGNED)
        with patch.object(
            CourseNodeRepository,
            "get_by_id",
            return_value=_mock_node(node_id=node_id),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents/upload-url",
                json={
                    "filename": "script.py",
                    "content_type": "text/x-python",
                    "source_type": "code",
                },
            )
        assert resp.status_code == 200

    async def test_code_with_non_code_extension_400(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """source_type=code with a non-code extension fast-fails pre-PUT."""
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/documents/upload-url",
            json={
                "filename": "movie.mp4",
                "content_type": "video/mp4",
                "source_type": "code",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["category"] == "forbidden_type"

    async def test_zip_with_non_code_source_type_422(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """zip is code-only — fast-fail BEFORE the client PUTs the bytes."""
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/documents/upload-url",
            json={
                "filename": "bundle.zip",
                "content_type": "application/zip",
                "source_type": "text",
            },
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "ARCHIVE_REQUIRES_CODE"

    async def test_404_node_not_found(self, client: AsyncClient) -> None:
        """Non-existent node returns 404."""
        with patch.object(CourseNodeRepository, "get_by_id", return_value=None):
            resp = await client.post(
                f"/api/v1/nodes/{uuid.uuid4()}/documents/upload-url",
                json={
                    "filename": "doc.md",
                    "content_type": "text/markdown",
                    "source_type": "text",
                },
            )
        assert resp.status_code == 404


# -- POST /nodes/{nid}/documents/confirm-upload --


class TestConfirmUpload:
    """Confirm presigned upload and create AuthoredDocument."""

    async def test_201_creates_entry(
        self,
        client: AsyncClient,
        node_id: uuid.UUID,
        mock_s3: AsyncMock,
    ) -> None:
        """Successful confirm creates AuthoredDocument with ingestion job."""
        mock_s3.head_object = AsyncMock(return_value={"ContentLength": 1024})
        # Phase 2.1 C8 — confirm_upload now fetches bytes for Stage 1.
        # Stage 1 itself is bypassed via the ``RUN_STAGE1_AT_DOCUMENTS``
        # patch below; any bytes value satisfies the await contract.
        mock_s3.get_object = AsyncMock(return_value=b"%PDF-1.4 dummy")
        mock_s3._endpoint_url = "http://localhost:9000"
        mock_s3._bucket = "course-materials"
        entry = _mock_entry(node_id=node_id)
        job = _mock_job()
        key = f"tenants/{STUB_TENANT.tenant_id}/nodes/{node_id}/abc/slides.pdf"

        with (
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(AuthoredDocumentRepository, "create", return_value=entry),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
            # Phase 2.1 C8 — Stage 1 fires on the confirm_upload path too.
            # This test focuses on the success persistence shape, not on
            # Stage 1 logic (covered by test_confirm_upload_stage1.py).
            patch(RUN_STAGE1_AT_DOCUMENTS, return_value=MagicMock()),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents/confirm-upload",
                json={
                    "key": key,
                    "source_type": "presentation",
                },
            )

        assert resp.status_code == 201
        assert resp.json()["job_id"] == str(job.id)

    async def test_code_confirm_201_skips_stage1(
        self,
        client: AsyncClient,
        node_id: uuid.UUID,
        mock_s3: AsyncMock,
    ) -> None:
        """confirm_upload mirrors the multipart code light-gate (no Stage 1)."""
        mock_s3.head_object = AsyncMock(return_value={"ContentLength": 64})
        mock_s3.get_object = AsyncMock(return_value=b'print("hello")\n')
        mock_s3._endpoint_url = "http://localhost:9000"
        mock_s3._bucket = "course-materials"
        entry = _mock_entry(node_id=node_id)
        job = _mock_job()
        key = f"tenants/{STUB_TENANT.tenant_id}/nodes/{node_id}/abc/script.py"

        with (
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(AuthoredDocumentRepository, "create", return_value=entry),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
            patch(RUN_STAGE1_AT_DOCUMENTS) as stage1_mock,
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents/confirm-upload",
                json={
                    "key": key,
                    "source_type": "code",
                },
            )

        assert resp.status_code == 201
        stage1_mock.assert_not_called()

    async def test_code_confirm_zip_with_non_code_source_type_422(
        self,
        client: AsyncClient,
        node_id: uuid.UUID,
        mock_s3: AsyncMock,
    ) -> None:
        """The zip↔code invariant holds on confirm even if upload-url was raced."""
        mock_s3.head_object = AsyncMock(return_value={"ContentLength": 64})
        mock_s3._endpoint_url = "http://localhost:9000"
        mock_s3._bucket = "course-materials"
        key = f"tenants/{STUB_TENANT.tenant_id}/nodes/{node_id}/abc/bundle.zip"

        with patch.object(
            CourseNodeRepository,
            "get_by_id",
            return_value=_mock_node(node_id=node_id),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents/confirm-upload",
                json={
                    "key": key,
                    "source_type": "text",
                },
            )

        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "ARCHIVE_REQUIRES_CODE"

    async def test_403_wrong_tenant_prefix(
        self,
        client: AsyncClient,
        node_id: uuid.UUID,
    ) -> None:
        """Key with wrong tenant prefix returns 403."""
        with patch.object(
            CourseNodeRepository,
            "get_by_id",
            return_value=_mock_node(node_id=node_id),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents/confirm-upload",
                json={
                    "key": "tenants/WRONG/nodes/x/file.pdf",
                    "source_type": "presentation",
                },
            )
        assert resp.status_code == 403

    async def test_404_file_not_in_s3(
        self,
        client: AsyncClient,
        node_id: uuid.UUID,
        mock_s3: AsyncMock,
    ) -> None:
        """File not found in S3 returns 404."""
        mock_s3.head_object = AsyncMock(side_effect=Exception("404"))
        key = f"tenants/{STUB_TENANT.tenant_id}/nodes/{node_id}/abc/gone.pdf"

        with patch.object(
            CourseNodeRepository,
            "get_by_id",
            return_value=_mock_node(node_id=node_id),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents/confirm-upload",
                json={
                    "key": key,
                    "source_type": "presentation",
                },
            )
        assert resp.status_code == 404
        assert "not found in S3" in resp.json()["detail"]
