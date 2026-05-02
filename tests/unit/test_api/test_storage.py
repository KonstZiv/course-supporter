"""Tests for storage management API endpoints (S3-021)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    plan_id="basic",
    key_prefix="cs_test",
)

_ENTRY_REPO = "course_supporter.api.routes.storage.AuthoredDocumentRepository"


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.fixture()
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3._endpoint_url = "http://localhost:9000"
    s3._bucket = "course-materials"
    return s3


@pytest.fixture()
def mock_arq() -> AsyncMock:
    """Mock ARQ Redis: ``enqueue_job`` returns a stub with a job_id.

    Used by ``delete_file`` after Phase 1 KD3 adoption — the handler
    hands off to ``enqueue_s3_cleanup`` which invokes ARQ. Tests
    typically patch the helper directly when they need to assert
    call shape; this fixture exists so the FastAPI dep injection
    succeeds when the helper is NOT patched.
    """
    arq = AsyncMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-test-id"))
    return arq


@pytest.fixture()
async def client(
    mock_session: AsyncMock, mock_s3: AsyncMock, mock_arq: AsyncMock
) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_arq_redis] = lambda: mock_arq
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestListFiles:
    async def test_200_returns_files(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """Returns list of tenant's files."""
        now = datetime.now(UTC)
        mock_s3.list_objects = AsyncMock(
            return_value=[
                {"key": "tenants/t/a.pdf", "size": 100, "last_modified": now},
                {"key": "tenants/t/b.mp4", "size": 200, "last_modified": now},
            ]
        )

        resp = await client.get("/api/v1/storage/files")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["key"] == "tenants/t/a.pdf"
        assert data[0]["size_bytes"] == 100
        assert data[1]["size_bytes"] == 200

    async def test_200_empty(self, client: AsyncClient, mock_s3: AsyncMock) -> None:
        """Returns empty list when no files."""
        mock_s3.list_objects = AsyncMock(return_value=[])

        resp = await client.get("/api/v1/storage/files")

        assert resp.status_code == 200
        assert resp.json() == []

    async def test_uses_tenant_prefix(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """Calls list_objects with tenant-scoped prefix."""
        mock_s3.list_objects = AsyncMock(return_value=[])

        await client.get("/api/v1/storage/files")

        prefix = mock_s3.list_objects.call_args[0][0]
        assert prefix == f"tenants/{STUB_TENANT.tenant_id}/"


class TestGetUsage:
    async def test_200_returns_usage(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """Returns total bytes and file count."""
        mock_s3.get_usage = AsyncMock(return_value=(1500, 3))

        resp = await client.get("/api/v1/storage/usage")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_bytes"] == 1500
        assert data["file_count"] == 3

    async def test_200_empty(self, client: AsyncClient, mock_s3: AsyncMock) -> None:
        """Empty storage returns zeros."""
        mock_s3.get_usage = AsyncMock(return_value=(0, 0))

        resp = await client.get("/api/v1/storage/usage")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_bytes"] == 0
        assert data["file_count"] == 0


class TestDeleteFile:
    """DELETE /api/v1/storage/files/{key} — Phase 1 commit (m) KD3 cascade
    + force-orphan s3_cleanup orchestration.

    Mirrors commit (k)'s ``TestDeleteNode`` and commit (l)'s
    ``TestDeleteDocument`` shape with cascade rooted at AuthoredDocument
    plus a force-orphan branch when no DB row references the URL. The
    handler now: (1) gates on tenant prefix BEFORE any DB or cascade
    work, (2) looks up the AuthoredDocument by source_url, (3) Mode A
    runs ``CascadeDeleteService.soft_delete_with_cascade`` (with
    ``on_invalidate_hashes`` hook bridging ``ContentHashService``)
    plus ``enqueue_s3_cleanup`` carrying ``course_node_id``, (4) Mode B
    skips cascade and enqueues with the single key only. All S3
    deletion is ARQ-deferred — never synchronous in-handler.
    """

    async def test_403_wrong_tenant_prefix(self, client: AsyncClient) -> None:
        """Key with cross-tenant prefix yields 403 BEFORE any DB lookup
        or cascade work. Locks the security gate as the first check.
        """
        key = "tenants/OTHER_TENANT/file.pdf"

        with (
            patch(_ENTRY_REPO) as repo_cls,
            patch(
                "course_supporter.api.routes.storage.enqueue_s3_cleanup",
                AsyncMock(),
            ) as enqueue_mock,
        ):
            repo_cls.return_value.get_by_source_url = AsyncMock()
            resp = await client.delete(f"/api/v1/storage/files/{key}")

        assert resp.status_code == 403
        # Tenant prefix gate must run FIRST — neither DB lookup nor
        # cascade nor enqueue should fire.
        repo_cls.return_value.get_by_source_url.assert_not_awaited()
        enqueue_mock.assert_not_awaited()

    async def test_204_with_db_row_cascades_and_enqueues(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """Mode A: AuthoredDocument exists → cascade fires + enqueue
        called with file_keys=[key] + tenant_id + course_node_id.
        Locks the QQ5 ordering at the unit level — cascade engine
        integration test in tests/storage/test_cascade_invalidation.py
        locks the scrub-then-collect-impossible failure mode at the
        live-engine level.
        """
        key = f"tenants/{STUB_TENANT.tenant_id}/nodes/n/file.pdf"
        course_node_id = uuid.uuid4()
        document_id = uuid.uuid4()

        document = MagicMock()
        document.id = document_id
        document.course_node_id = course_node_id
        document.source_url = f"http://localhost:9000/course-materials/{key}"

        cascade_mock = AsyncMock()
        enqueue_mock = AsyncMock()

        with (
            patch(_ENTRY_REPO) as repo_cls,
            patch(
                "course_supporter.storage.cascade.CascadeDeleteService."
                "soft_delete_with_cascade",
                cascade_mock,
            ),
            patch(
                "course_supporter.api.routes.storage.enqueue_s3_cleanup",
                enqueue_mock,
            ),
        ):
            repo_cls.return_value.get_by_source_url = AsyncMock(return_value=document)
            resp = await client.delete(f"/api/v1/storage/files/{key}")

        assert resp.status_code == 204
        cascade_mock.assert_awaited_once()
        enqueue_mock.assert_awaited_once()
        kwargs = enqueue_mock.call_args.kwargs
        assert kwargs["file_keys"] == [key]
        assert kwargs["tenant_id"] == STUB_TENANT.tenant_id
        assert kwargs["course_node_id"] == course_node_id

    async def test_204_force_orphan_no_db_row(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """Mode B: no AuthoredDocument referencing the URL → cascade
        NOT called; enqueue_s3_cleanup fires with file_keys=[key] +
        tenant_id (course_node_id absent — no DB row to attribute).
        Job row still created so cost-attribution + reactivate-flow
        remain eligible per KD13.
        """
        key = f"tenants/{STUB_TENANT.tenant_id}/orphan.pdf"

        cascade_mock = AsyncMock()
        enqueue_mock = AsyncMock()

        with (
            patch(_ENTRY_REPO) as repo_cls,
            patch(
                "course_supporter.storage.cascade.CascadeDeleteService."
                "soft_delete_with_cascade",
                cascade_mock,
            ),
            patch(
                "course_supporter.api.routes.storage.enqueue_s3_cleanup",
                enqueue_mock,
            ),
        ):
            repo_cls.return_value.get_by_source_url = AsyncMock(return_value=None)
            resp = await client.delete(f"/api/v1/storage/files/{key}")

        assert resp.status_code == 204
        # Force-orphan: no cascade work whatsoever.
        cascade_mock.assert_not_awaited()
        # Helper still fires for ARQ-deferred S3 cleanup.
        enqueue_mock.assert_awaited_once()
        kwargs = enqueue_mock.call_args.kwargs
        assert kwargs["file_keys"] == [key]
        assert kwargs["tenant_id"] == STUB_TENANT.tenant_id
        # No course_node_id passed (default None) — orphan path.
        assert kwargs.get("course_node_id") is None

    async def test_handler_never_calls_synchronous_s3_delete(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """Both modes route S3 deletion through ARQ worker. The handler
        itself MUST NOT call ``s3.delete_object`` synchronously — that
        was the legacy behavior, removed in commit (m). Eventually-
        consistent S3 deletion is the QQ5 contract.
        """
        key = f"tenants/{STUB_TENANT.tenant_id}/file.pdf"

        with (
            patch(_ENTRY_REPO) as repo_cls,
            patch(
                "course_supporter.storage.cascade.CascadeDeleteService."
                "soft_delete_with_cascade",
                AsyncMock(),
            ),
            patch(
                "course_supporter.api.routes.storage.enqueue_s3_cleanup",
                AsyncMock(),
            ),
        ):
            repo_cls.return_value.get_by_source_url = AsyncMock(return_value=None)
            await client.delete(f"/api/v1/storage/files/{key}")

        mock_s3.delete_object.assert_not_awaited()
