"""Tests for storage management API endpoints (S3-021)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)

_ENTRY_REPO = "course_supporter.api.routes.storage.MaterialEntryRepository"


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
    async def test_204_deletes_file_with_cascade(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """Deletes S3 file and cascades to MaterialEntry."""
        key = f"tenants/{STUB_TENANT.tenant_id}/nodes/n/file.pdf"
        mock_entry = MagicMock()
        mock_entry.id = uuid.uuid4()

        with patch(_ENTRY_REPO) as repo_cls:
            repo_cls.return_value.get_by_source_url = AsyncMock(return_value=mock_entry)
            repo_cls.return_value.delete = AsyncMock()
            resp = await client.delete(f"/api/v1/storage/files/{key}")

        assert resp.status_code == 204
        mock_s3.delete_object.assert_awaited_once_with(key)
        repo_cls.return_value.delete.assert_awaited_once_with(mock_entry.id)

    async def test_204_no_entry_still_deletes_s3(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """Deletes S3 file even when no MaterialEntry exists."""
        key = f"tenants/{STUB_TENANT.tenant_id}/orphan.pdf"

        with patch(_ENTRY_REPO) as repo_cls:
            repo_cls.return_value.get_by_source_url = AsyncMock(return_value=None)
            resp = await client.delete(f"/api/v1/storage/files/{key}")

        assert resp.status_code == 204
        mock_s3.delete_object.assert_awaited_once()

    async def test_403_wrong_tenant(self, client: AsyncClient) -> None:
        """Key belonging to another tenant returns 403."""
        key = "tenants/OTHER_TENANT/file.pdf"

        resp = await client.delete(f"/api/v1/storage/files/{key}")

        assert resp.status_code == 403
