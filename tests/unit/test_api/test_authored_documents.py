"""Tests for authored document API endpoints (tree-attached documents)."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.jobs.cancellation_service import JobCancellationService
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.database import get_session

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)

ENQUEUE_FUNC = "course_supporter.api.routes.documents.enqueue_ingestion"


def _mock_node(
    *,
    node_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock CourseNode with tenant_id."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.tenant_id = tenant_id or STUB_TENANT.tenant_id
    return node


def _mock_entry(
    *,
    entry_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    source_type: str = "text",
    material_role: str = "educational",
    task_type: str | None = None,
    source_url: str = "https://example.com/doc.md",
    filename: str | None = None,
    order: int = 0,
    state: str = "raw",
    error_message: str | None = None,
    job_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock AuthoredDocument with ORM-compatible attributes."""
    entry = MagicMock()
    entry.id = entry_id or uuid.uuid4()
    entry.course_node_id = node_id or uuid.uuid4()
    entry.source_type = source_type
    entry.material_role = material_role
    entry.task_type = task_type
    entry.source_url = source_url
    entry.filename = filename
    entry.language = None
    entry.order = order
    entry.state = state
    entry.error_message = error_message
    entry.job_id = job_id
    entry.deleted_at = None
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
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.upload_smart = AsyncMock(
        return_value=("http://localhost:9000/course-materials/key/file.pdf", 1024)
    )
    s3.extract_key = MagicMock(return_value=None)
    return s3


@pytest.fixture()
def node_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
async def client(
    mock_session: AsyncMock, mock_arq: MagicMock, mock_s3: AsyncMock
) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_arq_redis] = lambda: mock_arq
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestCreateDocument:
    """POST /api/v1/nodes/{nid}/documents"""

    async def test_returns_201_with_url(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Successful document creation with URL returns 201 with job_id."""
        entry = _mock_entry(node_id=node_id)
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
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents",
                data={
                    "source_type": "text",
                    "source_url": "https://example.com/doc.md",
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == str(entry.id)
        assert data["job_id"] == str(job.id)
        assert data["source_type"] == "text"

    async def test_returns_201_with_file_upload(
        self,
        client: AsyncClient,
        node_id: uuid.UUID,
        mock_s3: AsyncMock,
    ) -> None:
        """Successful file upload returns 201."""
        entry = _mock_entry(
            node_id=node_id,
            source_type="presentation",
            source_url="http://localhost:9000/course-materials/key/file.pdf",
            filename="slides.pdf",
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
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents",
                data={"source_type": "presentation"},
                files={
                    "file": (
                        "slides.pdf",
                        io.BytesIO(b"PDF content"),
                        "application/pdf",
                    )
                },
            )
        assert resp.status_code == 201
        mock_s3.upload_smart.assert_awaited_once()

    async def test_invalid_source_type_returns_422(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Invalid source_type is rejected by validation."""
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/documents",
            data={
                "source_type": "invalid",
                "source_url": "https://example.com/doc.md",
            },
        )
        assert resp.status_code == 422

    async def test_no_url_no_file_returns_422(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Neither URL nor file provided returns 422."""
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/documents",
            data={"source_type": "text"},
        )
        assert resp.status_code == 422
        assert "Either source_url or file" in resp.json()["detail"]

    async def test_web_rejects_file_upload(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """source_type 'web' does not accept file uploads."""
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/documents",
            data={"source_type": "web"},
            files={
                "file": ("page.html", io.BytesIO(b"<html>"), "text/html"),
            },
        )
        assert resp.status_code == 422
        assert "does not accept file uploads" in resp.json()["detail"]

    async def test_invalid_extension_returns_422(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """File with wrong extension for source_type returns 422."""
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/documents",
            data={"source_type": "video"},
            files={
                "file": (
                    "slides.pdf",
                    io.BytesIO(b"PDF content"),
                    "application/pdf",
                ),
            },
        )
        assert resp.status_code == 422
        assert "'.pdf' is not allowed" in resp.json()["detail"]

    async def test_node_not_found_returns_404(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Non-existent node returns 404."""
        with patch.object(CourseNodeRepository, "get_by_id", return_value=None):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents",
                data={
                    "source_type": "text",
                    "source_url": "https://example.com/doc.md",
                },
            )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Node not found"

    async def test_node_wrong_tenant_returns_404(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Node belonging to another tenant returns 404."""
        other_tenant = uuid.uuid4()
        with patch.object(
            CourseNodeRepository,
            "get_by_id",
            return_value=_mock_node(node_id=node_id, tenant_id=other_tenant),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents",
                data={
                    "source_type": "text",
                    "source_url": "https://example.com/doc.md",
                },
            )
        assert resp.status_code == 404

    async def test_with_filename_override(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Creation with filename override includes it in response."""
        entry = _mock_entry(node_id=node_id, filename="notes.md")
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
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/documents",
                data={
                    "source_type": "text",
                    "source_url": "https://example.com/notes.md",
                    "filename": "notes.md",
                },
            )
        assert resp.status_code == 201
        assert resp.json()["filename"] == "notes.md"


class TestCreateDocumentOpenAPISpec:
    """Regression: ensure create_document endpoint accepts multipart/form-data."""

    async def test_openapi_content_type_is_multipart(self, client: AsyncClient) -> None:
        """File upload endpoint must declare multipart/form-data content type."""
        resp = await client.get("/openapi.json")
        schema = resp.json()
        path = "/api/v1/nodes/{node_id}/documents"
        content_types = list(
            schema["paths"][path]["post"]["requestBody"]["content"].keys()
        )
        assert "multipart/form-data" in content_types


class TestListDocuments:
    """GET /api/v1/nodes/{nid}/documents"""

    async def test_returns_list(self, client: AsyncClient, node_id: uuid.UUID) -> None:
        """Returns list of documents for the node."""
        entries = [
            _mock_entry(node_id=node_id, order=0),
            _mock_entry(node_id=node_id, order=1, source_type="video"),
        ]
        with (
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(
                AuthoredDocumentRepository, "get_for_node", return_value=entries
            ),
        ):
            resp = await client.get(f"/api/v1/nodes/{node_id}/documents")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["order"] == 0
        assert data[1]["source_type"] == "video"

    async def test_empty_list(self, client: AsyncClient, node_id: uuid.UUID) -> None:
        """Returns empty list when node has no documents."""
        with (
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(AuthoredDocumentRepository, "get_for_node", return_value=[]),
        ):
            resp = await client.get(f"/api/v1/nodes/{node_id}/documents")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_node_not_found_returns_404(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Non-existent node returns 404."""
        with patch.object(CourseNodeRepository, "get_by_id", return_value=None):
            resp = await client.get(f"/api/v1/nodes/{node_id}/documents")
        assert resp.status_code == 404


class TestGetDocument:
    """GET /api/v1/documents/{did}"""

    async def test_returns_entry(self, client: AsyncClient, node_id: uuid.UUID) -> None:
        """Returns single authored document."""
        entry = _mock_entry(node_id=node_id, state="ready")
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
        ):
            resp = await client.get(f"/api/v1/documents/{entry.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(entry.id)
        assert data["state"] == "ready"

    async def test_not_found_returns_404(self, client: AsyncClient) -> None:
        """Non-existent document returns 404."""
        with patch.object(AuthoredDocumentRepository, "get_by_id", return_value=None):
            resp = await client.get(f"/api/v1/documents/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_wrong_tenant_returns_404(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Document belonging to another tenant returns 404."""
        entry = _mock_entry(node_id=node_id)
        other_tenant = uuid.uuid4()
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, tenant_id=other_tenant),
            ),
        ):
            resp = await client.get(f"/api/v1/documents/{entry.id}")
        assert resp.status_code == 404


class TestDeleteDocument:
    """DELETE /api/v1/documents/{did} — Phase 1 commit (l) KD3 cascade
    soft-delete + s3_cleanup orchestration.

    Mirrors commit (k)'s ``TestDeleteNode`` shape with the cascade
    rooted at AuthoredDocument instead of CourseNode. The handler now:
    (1) extracts the S3 key from ``document.source_url`` BEFORE
    cascade fires (cascade scrub clears ``source_url`` to ``""``),
    (2) issues ``CascadeDeleteService.soft_delete_with_cascade`` which
    auto-dispatches ``__scrub_callable__`` per victim type
    (AuthoredDocument scrubbed; DocumentSummary + DocumentSegment
    descendants no-op in Phase 1 per Amendment 16), and (3) hands off
    to ``enqueue_s3_cleanup`` which owns the QQ5 commit boundary.
    """

    async def test_returns_204_when_no_s3_key(
        self, client: AsyncClient, node_id: uuid.UUID, mock_s3: AsyncMock
    ) -> None:
        """External URL (extract_key returns None) — handler short-circuits
        to direct ``session.commit()``; no enqueue, still 204.
        """
        entry = _mock_entry(node_id=node_id, source_url="https://example.com/doc.md")
        mock_s3.extract_key = MagicMock(return_value=None)
        cascade_mock = AsyncMock()
        enqueue_mock = AsyncMock()
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch(
                "course_supporter.storage.cascade.CascadeDeleteService."
                "soft_delete_with_cascade",
                cascade_mock,
            ),
            patch(
                "course_supporter.api.routes.documents.enqueue_s3_cleanup",
                enqueue_mock,
            ),
        ):
            resp = await client.delete(f"/api/v1/documents/{entry.id}")
        assert resp.status_code == 204
        cascade_mock.assert_awaited_once()
        enqueue_mock.assert_not_awaited()

    async def test_not_found_returns_404(self, client: AsyncClient) -> None:
        """Non-existent document returns 404."""
        with patch.object(AuthoredDocumentRepository, "get_by_id", return_value=None):
            resp = await client.delete(f"/api/v1/documents/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_wrong_tenant_returns_404(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Document belonging to another tenant returns 404."""
        entry = _mock_entry(node_id=node_id)
        other_tenant = uuid.uuid4()
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, tenant_id=other_tenant),
            ),
        ):
            resp = await client.delete(f"/api/v1/documents/{entry.id}")
        assert resp.status_code == 404

    async def test_collects_key_before_cascade_and_enqueues_cleanup(
        self,
        client: AsyncClient,
        node_id: uuid.UUID,
        mock_s3: AsyncMock,
    ) -> None:
        """File key extracted from ``document.source_url`` and forwarded
        to ``enqueue_s3_cleanup`` along with tenant/course_node anchors.
        Locks the QQ5 ordering (collect → cascade → enqueue) at the
        unit level — the cascade engine integration test in
        ``tests/storage/test_cascade_invalidation.py`` locks the
        scrub-then-collect-impossible failure mode.
        """
        entry = _mock_entry(
            node_id=node_id,
            source_url="http://localhost:9000/bucket/tenants/t/file.pdf",
        )
        mock_s3.extract_key = MagicMock(return_value="tenants/t/file.pdf")
        cascade_mock = AsyncMock()
        enqueue_mock = AsyncMock()
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch(
                "course_supporter.storage.cascade.CascadeDeleteService."
                "soft_delete_with_cascade",
                cascade_mock,
            ),
            patch(
                "course_supporter.api.routes.documents.enqueue_s3_cleanup",
                enqueue_mock,
            ),
        ):
            resp = await client.delete(f"/api/v1/documents/{entry.id}")
        assert resp.status_code == 204
        cascade_mock.assert_awaited_once()
        enqueue_mock.assert_awaited_once()
        kwargs = enqueue_mock.call_args.kwargs
        assert kwargs["file_keys"] == ["tenants/t/file.pdf"]
        assert kwargs["course_node_id"] == node_id
        assert kwargs["tenant_id"] == STUB_TENANT.tenant_id

    async def test_no_enqueue_when_extract_key_returns_none(
        self,
        client: AsyncClient,
        node_id: uuid.UUID,
        mock_s3: AsyncMock,
    ) -> None:
        """External URL yields ``None`` from ``extract_key`` — handler
        skips ``enqueue_s3_cleanup`` and commits the cascade directly.
        Avoids creating a wasteful Job row + ARQ task with empty payload.
        """
        entry = _mock_entry(node_id=node_id, source_url="https://example.com/video.mp4")
        mock_s3.extract_key = MagicMock(return_value=None)
        cascade_mock = AsyncMock()
        enqueue_mock = AsyncMock()
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch(
                "course_supporter.storage.cascade.CascadeDeleteService."
                "soft_delete_with_cascade",
                cascade_mock,
            ),
            patch(
                "course_supporter.api.routes.documents.enqueue_s3_cleanup",
                enqueue_mock,
            ),
        ):
            resp = await client.delete(f"/api/v1/documents/{entry.id}")
        assert resp.status_code == 204
        cascade_mock.assert_awaited_once()
        enqueue_mock.assert_not_awaited()

    async def test_passes_cancel_hook_with_course_node_augmentation(
        self,
        client: AsyncClient,
        node_id: uuid.UUID,
        mock_s3: AsyncMock,
    ) -> None:
        """Hotfix-5 contract — handler wires ``on_cancel_jobs`` as a
        closure that augments the cascade-engine victim id list with
        ``document.course_node_id`` before forwarding to JCS. JCS lookup
        paths are course_node_id-keyed; raw victim ids (just the
        document id) would silent-no-op. Closure invocation invokes
        :meth:`JobCancellationService.cancel_jobs_for_entities` with
        ``[document_id, course_node_id]``.
        """
        entry = _mock_entry(
            node_id=node_id,
            source_url="https://example.com/doc.md",
        )
        mock_s3.extract_key = MagicMock(return_value=None)
        cascade_mock = AsyncMock()
        jcs_method_mock = AsyncMock()
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch(
                "course_supporter.storage.cascade.CascadeDeleteService."
                "soft_delete_with_cascade",
                cascade_mock,
            ),
            patch.object(
                JobCancellationService,
                "cancel_jobs_for_entities",
                jcs_method_mock,
            ),
        ):
            resp = await client.delete(f"/api/v1/documents/{entry.id}")
            assert resp.status_code == 204
            cascade_mock.assert_awaited_once()
            # Closure passed (not the bound JCS method directly — that
            # is the nodes.py pattern; documents.py uses closure
            # augmentation).
            on_cancel_jobs = cascade_mock.call_args.kwargs.get("on_cancel_jobs")
            assert on_cancel_jobs is not None, "on_cancel_jobs missing — KD13 gap"
            assert not hasattr(on_cancel_jobs, "__func__") or (
                on_cancel_jobs.__func__
                is not (JobCancellationService.cancel_jobs_for_entities)
            ), (
                "delete_document must use closure augmentation, not "
                "direct-bind — see KD13 closure-augmentation rationale "
                "in route docstring"
            )
            # Invoke the closure with sample victim ids and assert the
            # augmented call to JCS includes course_node_id. MUST run
            # inside the patch.object block so the JCS class patch is
            # still active when the closure dispatches the call.
            sample_victims = [entry.id]
            await on_cancel_jobs(sample_victims)
            jcs_method_mock.assert_awaited_once()
            passed_ids = jcs_method_mock.call_args.args[0]
            assert entry.id in passed_ids, "victim id stripped by augmentation"
            assert node_id in passed_ids, (
                "course_node_id missing from augmented ids — "
                "JCS lookup would silent-no-op"
            )

    async def test_passes_on_invalidate_hashes_hook(
        self,
        client: AsyncClient,
        node_id: uuid.UUID,
        mock_s3: AsyncMock,
    ) -> None:
        """Hotfix-5 contract — closes tangential gap from commit (l)
        where ``on_invalidate_hashes`` was omitted entirely from
        ``delete_document`` (mirrors of this hook are present in
        ``delete_node`` (commit (k)) and ``delete_file`` (commit (m))).
        Regression guard: a future revert to a 2-arg cascade call would
        re-introduce stale parent-CourseNode content_hash bug.
        """
        entry = _mock_entry(
            node_id=node_id,
            source_url="https://example.com/doc.md",
        )
        mock_s3.extract_key = MagicMock(return_value=None)
        cascade_mock = AsyncMock()
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch(
                "course_supporter.storage.cascade.CascadeDeleteService."
                "soft_delete_with_cascade",
                cascade_mock,
            ),
        ):
            resp = await client.delete(f"/api/v1/documents/{entry.id}")
        assert resp.status_code == 204
        cascade_mock.assert_awaited_once()
        on_invalidate_hashes = cascade_mock.call_args.kwargs.get("on_invalidate_hashes")
        assert on_invalidate_hashes is not None, (
            "on_invalidate_hashes missing — tangential gap from commit "
            "(l) regressed; parent-CourseNode content_hash recompute "
            "would treat the document as still present"
        )


class TestRetryDocument:
    """POST /api/v1/documents/{did}/retry"""

    async def test_returns_200_with_new_job(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Successful retry returns 200 with new job_id."""
        entry = _mock_entry(
            node_id=node_id,
            state="error",
            error_message="Processing failed",
        )
        job = _mock_job()
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            resp = await client.post(f"/api/v1/documents/{entry.id}/retry")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == str(job.id)

    async def test_non_error_state_returns_409(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Retry on non-error document returns 409."""
        entry = _mock_entry(node_id=node_id, state="ready")
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
        ):
            resp = await client.post(f"/api/v1/documents/{entry.id}/retry")
        assert resp.status_code == 409
        assert "ready" in resp.json()["detail"]

    async def test_pending_state_returns_409(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Retry on pending document returns 409."""
        entry = _mock_entry(node_id=node_id, state="pending")
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
        ):
            resp = await client.post(f"/api/v1/documents/{entry.id}/retry")
        assert resp.status_code == 409

    async def test_not_found_returns_404(self, client: AsyncClient) -> None:
        """Non-existent document returns 404."""
        with patch.object(AuthoredDocumentRepository, "get_by_id", return_value=None):
            resp = await client.post(f"/api/v1/documents/{uuid.uuid4()}/retry")
        assert resp.status_code == 404

    async def test_returns_410_when_soft_deleted(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """QQ6: retry on a soft-deleted document yields HTTP 410 Gone.

        Fires BEFORE the state-check branch — even an ``error``-state
        soft-deleted row returns 410, never 409. The row's been
        logically removed; re-ingestion is not a recovery path.
        """
        entry = _mock_entry(
            node_id=node_id,
            state="error",
            error_message="Processing failed",
        )
        entry.deleted_at = datetime.now(UTC)
        enqueue_mock = AsyncMock()
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch(ENQUEUE_FUNC, enqueue_mock),
        ):
            resp = await client.post(f"/api/v1/documents/{entry.id}/retry")
        assert resp.status_code == 410
        assert "deleted" in resp.json()["detail"].lower()
        # Retry orchestration must NOT fire when soft-deleted.
        enqueue_mock.assert_not_awaited()


class TestUpdateDocument:
    """PATCH /api/v1/documents/{did}"""

    async def test_updates_role_to_methodological(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Successful role update returns 200 with new role."""
        entry = _mock_entry(node_id=node_id, material_role="educational")
        updated = _mock_entry(node_id=node_id, material_role="methodological")
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(
                AuthoredDocumentRepository,
                "update_material_role",
                return_value=updated,
            ),
        ):
            resp = await client.patch(
                f"/api/v1/documents/{entry.id}",
                json={"material_role": "methodological"},
            )
        assert resp.status_code == 200
        assert resp.json()["material_role"] == "methodological"

    async def test_updates_role_to_educational(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Toggle back to educational."""
        entry = _mock_entry(node_id=node_id, material_role="methodological")
        updated = _mock_entry(node_id=node_id, material_role="educational")
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(
                AuthoredDocumentRepository,
                "update_material_role",
                return_value=updated,
            ),
        ):
            resp = await client.patch(
                f"/api/v1/documents/{entry.id}",
                json={"material_role": "educational"},
            )
        assert resp.status_code == 200
        assert resp.json()["material_role"] == "educational"

    async def test_invalid_role_returns_422(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Invalid material_role value returns 422."""
        resp = await client.patch(
            f"/api/v1/documents/{uuid.uuid4()}",
            json={"material_role": "invalid"},
        )
        assert resp.status_code == 422

    async def test_set_task_type(self, client: AsyncClient, node_id: uuid.UUID) -> None:
        """PATCH sets task_type without changing role."""
        entry = _mock_entry(node_id=node_id, material_role="educational")
        updated = _mock_entry(
            node_id=node_id,
            material_role="educational",
            task_type="short_task",
        )
        update_task_mock = AsyncMock(return_value=updated)
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(
                AuthoredDocumentRepository,
                "update_task_type",
                update_task_mock,
            ),
        ):
            resp = await client.patch(
                f"/api/v1/documents/{entry.id}",
                json={"task_type": "short_task"},
            )
        assert resp.status_code == 200
        assert resp.json()["task_type"] == "short_task"
        # material_role update should NOT have been called since it wasn't in body
        update_task_mock.assert_awaited_once()

    async def test_clear_task_type(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """PATCH with task_type: null clears the task flag."""
        entry = _mock_entry(
            node_id=node_id,
            material_role="educational",
            task_type="task",
        )
        updated = _mock_entry(
            node_id=node_id,
            material_role="educational",
            task_type=None,
        )
        update_task_mock = AsyncMock(return_value=updated)
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
            patch.object(
                AuthoredDocumentRepository,
                "update_task_type",
                update_task_mock,
            ),
        ):
            resp = await client.patch(
                f"/api/v1/documents/{entry.id}",
                json={"task_type": None},
            )
        assert resp.status_code == 200
        assert resp.json()["task_type"] is None
        update_task_mock.assert_awaited_once()

    async def test_empty_body_returns_422(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """PATCH with no fields returns 422."""
        entry = _mock_entry(node_id=node_id)
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id),
            ),
        ):
            resp = await client.patch(
                f"/api/v1/documents/{entry.id}",
                json={},
            )
        assert resp.status_code == 422

    async def test_invalid_task_type_returns_422(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """PATCH rejects task_type outside the taxonomy."""
        resp = await client.patch(
            f"/api/v1/documents/{uuid.uuid4()}",
            json={"task_type": "essay"},
        )
        assert resp.status_code == 422

    async def test_not_found_returns_404(self, client: AsyncClient) -> None:
        """Non-existent document returns 404."""
        with patch.object(AuthoredDocumentRepository, "get_by_id", return_value=None):
            resp = await client.patch(
                f"/api/v1/documents/{uuid.uuid4()}",
                json={"material_role": "methodological"},
            )
        assert resp.status_code == 404

    async def test_wrong_tenant_returns_404(
        self, client: AsyncClient, node_id: uuid.UUID
    ) -> None:
        """Document belonging to another tenant returns 404."""
        entry = _mock_entry(node_id=node_id)
        other_tenant = uuid.uuid4()
        with (
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=entry),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_node(node_id=node_id, tenant_id=other_tenant),
            ),
        ):
            resp = await client.patch(
                f"/api/v1/documents/{entry.id}",
                json={"material_role": "methodological"},
            )
        assert resp.status_code == 404
