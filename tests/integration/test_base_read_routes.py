"""Route tests for the KD18 P2 base read-contract.

* GET /api/v1/homework/tasks/{authored_document_id}/base (CheckDep, mode-1):
  latest-READY → its fields + presigned original_url; no-READY → latest version
  + its state (null snapshot_hash); 404 when the task has no base at all.
* GET /api/v1/documents/{document_id}/base/manifest (PrepDep, author-only):
  latest-READY manifest as-is (asdict shape); 404 when no READY version.

Real PostgreSQL; S3 presigned generation is a mock (its mechanics are T3-tested).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import AuthoredDocument, CourseNode, Tenant
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_READY_MANIFEST: dict[str, Any] = {
    "schema": 1,
    "aggregate_hash": "a" * 64,
    "included": [{"path": "main.py", "size": 10, "hash": "b" * 64, "cls": "text"}],
    "excluded": [
        {"path": ".venv/", "reason": "denylist_dir", "entries": 3, "size": 99}
    ],
    "total_files": 4,
    "total_bytes": 109,
}


@pytest.fixture()
async def project_env(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    async with session_factory() as session:
        tenant = Tenant(name=f"pb-read-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="Read", order=0)
        session.add(node)
        await session.flush()
        doc = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="s3://x",
            task_type="project",
        )
        session.add(doc)
        await session.flush()
        await session.commit()
        ids = {"tenant_id": tenant.id, "node_id": node.id, "doc_id": doc.id}
    yield ids
    async with session_factory() as session:
        await session.execute(
            delete(AuthoredDocument).where(
                AuthoredDocument.course_node_id == ids["node_id"]
            )
        )
        await session.execute(delete(CourseNode).where(CourseNode.id == ids["node_id"]))
        await session.execute(delete(Tenant).where(Tenant.id == ids["tenant_id"]))
        await session.commit()


@pytest.fixture()
def _wire(
    project_env: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> Generator[AsyncMock]:
    s3_mock = AsyncMock()
    s3_mock.generate_presigned_get_url = AsyncMock(return_value="https://minio/presig")

    tenant = TenantContext(
        tenant_id=project_env["tenant_id"],
        tenant_name="pb-read",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_test",
    )

    async def _override_session() -> Any:
        async with session_factory() as session:
            yield session

    async def _override_tenant() -> TenantContext:
        return tenant

    async def _override_s3() -> AsyncMock:
        return s3_mock

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_tenant] = _override_tenant
    app.dependency_overrides[get_s3_client] = _override_s3
    yield s3_mock
    app.dependency_overrides.clear()


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    doc_id: uuid.UUID,
    *,
    ready: bool,
    archive_key: str = "k/v1/original.zip",
) -> uuid.UUID:
    async with factory() as session:
        repo = ProjectBaseRepository(session)
        base = await repo.create_version(
            authored_document_id=doc_id, archive_key=archive_key
        )
        if ready:
            await repo.mark_ready(
                base.id,
                snapshot_key="k/v1/snapshot.zip",
                snapshot_hash="c" * 64,
                manifest=_READY_MANIFEST,
            )
        await session.commit()
        return base.id


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestGetTaskBase:
    async def test_ready_returns_hash_and_presigned(
        self,
        _wire: AsyncMock,
        project_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        doc_id = project_env["doc_id"]
        await _seed(session_factory, doc_id, ready=True)
        async with _client() as client:
            resp = await client.get(f"/api/v1/homework/tasks/{doc_id}/base")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "ready"
        assert body["version"] == 1
        assert body["snapshot_hash"] == "c" * 64
        assert body["original_url"] == "https://minio/presig"

    async def test_ready_wins_over_later_pending(
        self,
        _wire: AsyncMock,
        project_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """READY v1 + pending v2 → the descriptor is the active READY v1."""
        doc_id = project_env["doc_id"]
        await _seed(session_factory, doc_id, ready=True)
        await _seed(
            session_factory, doc_id, ready=False, archive_key="k/v2/original.zip"
        )
        async with _client() as client:
            resp = await client.get(f"/api/v1/homework/tasks/{doc_id}/base")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 1
        assert body["state"] == "ready"

    async def test_no_ready_returns_latest_state_null_hash(
        self,
        _wire: AsyncMock,
        project_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        doc_id = project_env["doc_id"]
        await _seed(session_factory, doc_id, ready=False)
        async with _client() as client:
            resp = await client.get(f"/api/v1/homework/tasks/{doc_id}/base")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "pending"
        assert body["snapshot_hash"] is None

    async def test_no_base_404(
        self,
        _wire: AsyncMock,
        project_env: dict[str, uuid.UUID],
    ) -> None:
        doc_id = project_env["doc_id"]
        async with _client() as client:
            resp = await client.get(f"/api/v1/homework/tasks/{doc_id}/base")
        assert resp.status_code == 404

    async def test_unknown_task_404(self, _wire: AsyncMock) -> None:
        async with _client() as client:
            resp = await client.get(f"/api/v1/homework/tasks/{uuid.uuid4()}/base")
        assert resp.status_code == 404


class TestGetBaseManifest:
    async def test_ready_returns_asdict_manifest(
        self,
        _wire: AsyncMock,
        project_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        doc_id = project_env["doc_id"]
        await _seed(session_factory, doc_id, ready=True)
        async with _client() as client:
            resp = await client.get(f"/api/v1/documents/{doc_id}/base/manifest")
        assert resp.status_code == 200, resp.text
        manifest = resp.json()
        assert manifest["schema"] == 1
        assert manifest["total_files"] == 4
        assert manifest["included"][0]["cls"] == "text"
        assert manifest["excluded"][0]["reason"] == "denylist_dir"

    async def test_no_ready_404(
        self,
        _wire: AsyncMock,
        project_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        doc_id = project_env["doc_id"]
        await _seed(session_factory, doc_id, ready=False)
        async with _client() as client:
            resp = await client.get(f"/api/v1/documents/{doc_id}/base/manifest")
        assert resp.status_code == 404

    async def test_no_base_404(
        self,
        _wire: AsyncMock,
        project_env: dict[str, uuid.UUID],
    ) -> None:
        doc_id = project_env["doc_id"]
        async with _client() as client:
            resp = await client.get(f"/api/v1/documents/{doc_id}/base/manifest")
        assert resp.status_code == 404
