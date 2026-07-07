"""Route tests for POST /api/v1/documents/{id}/base (KD18 P2).

Full FastAPI request pipeline (auth + tenant + document ownership) against real
PostgreSQL; S3 and ARQ are dependency-overridden mocks (the full real-MinIO +
real-ARQ end-to-end is the commit-6 HTTP-live gesture). Covers: 202 happy path
(pending version + enqueue), the task_type / archive-kind 422 gates, the
streamed 413 size cap, and the IntegrityError → 409 collision mapping.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.api.routes import documents as documents_module
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    Job,
    ProjectBase,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


@pytest.fixture()
async def project_env(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """A tenant + node + a project doc + a non-project doc (committed)."""
    async with session_factory() as session:
        tenant = Tenant(name=f"pb-route-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="Route", order=0)
        session.add(node)
        await session.flush()
        proj = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="s3://x",
            task_type="project",
        )
        task = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="s3://y",
            task_type="task",
        )
        session.add_all([proj, task])
        await session.flush()
        await session.commit()
        ids = {
            "tenant_id": tenant.id,
            "node_id": node.id,
            "project_doc_id": proj.id,
            "task_doc_id": task.id,
        }
    yield ids
    async with session_factory() as session:
        await session.execute(delete(Job).where(Job.tenant_id == ids["tenant_id"]))
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
) -> Generator[tuple[AsyncMock, AsyncMock]]:
    s3_mock = AsyncMock()
    s3_mock.upload_file = AsyncMock(return_value="s3://stored")
    arq_mock = AsyncMock()
    arq_mock.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq:base:1"))

    tenant = TenantContext(
        tenant_id=project_env["tenant_id"],
        tenant_name="pb-route",
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

    async def _override_arq() -> AsyncMock:
        return arq_mock

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_tenant] = _override_tenant
    app.dependency_overrides[get_s3_client] = _override_s3
    app.dependency_overrides[get_arq_redis] = _override_arq
    yield s3_mock, arq_mock
    app.dependency_overrides.clear()


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.py", "print('hi')\n")
    return buf.getvalue()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestAttachProjectBase:
    async def test_happy_path_202(
        self,
        _wire: tuple[AsyncMock, AsyncMock],
        project_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        s3_mock, arq_mock = _wire
        doc_id = project_env["project_doc_id"]
        async with _client() as client:
            resp = await client.post(
                f"/api/v1/documents/{doc_id}/base",
                files={"file": ("base.zip", _zip_bytes(), "application/zip")},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["version"] == 1
        assert body["state"] == "pending"
        base_version_id = uuid.UUID(body["base_version_id"])

        # Stored to S3 at a uuid-scoped key ending original.zip.
        s3_mock.upload_file.assert_awaited_once()
        stored_key = s3_mock.upload_file.await_args.args[0]
        assert stored_key.endswith("/original.zip")
        assert f"bases/{doc_id}/" in stored_key
        # Enqueued (base_normalize_task, job_id, project_base_id).
        arq_mock.enqueue_job.assert_awaited_once()
        enqueue_args = arq_mock.enqueue_job.await_args.args
        assert enqueue_args[0] == "base_normalize_task"
        assert enqueue_args[2] == str(base_version_id)

        # The pending version is durably committed.
        async with session_factory() as session:
            base = await session.get(ProjectBase, base_version_id)
            assert base is not None
            assert base.state == "pending"
            assert base.version == 1
            assert base.snapshot_key is None
            assert base.archive_key.endswith("/original.zip")

    async def test_non_project_doc_422(
        self,
        _wire: tuple[AsyncMock, AsyncMock],
        project_env: dict[str, uuid.UUID],
    ) -> None:
        s3_mock, _ = _wire
        doc_id = project_env["task_doc_id"]
        async with _client() as client:
            resp = await client.post(
                f"/api/v1/documents/{doc_id}/base",
                files={"file": ("base.zip", _zip_bytes(), "application/zip")},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "NOT_A_PROJECT_TASK"
        s3_mock.upload_file.assert_not_awaited()

    async def test_non_archive_extension_422(
        self,
        _wire: tuple[AsyncMock, AsyncMock],
        project_env: dict[str, uuid.UUID],
    ) -> None:
        s3_mock, _ = _wire
        doc_id = project_env["project_doc_id"]
        async with _client() as client:
            resp = await client.post(
                f"/api/v1/documents/{doc_id}/base",
                files={"file": ("notes.pdf", b"%PDF-1.4 fake", "application/pdf")},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "NOT_AN_ARCHIVE"
        s3_mock.upload_file.assert_not_awaited()

    async def test_oversize_413(
        self,
        _wire: tuple[AsyncMock, AsyncMock],
        project_env: dict[str, uuid.UUID],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        s3_mock, _ = _wire
        monkeypatch.setattr(documents_module, "_BASE_ARCHIVE_MAX_UPLOAD_BYTES", 1024)
        doc_id = project_env["project_doc_id"]
        async with _client() as client:
            resp = await client.post(
                f"/api/v1/documents/{doc_id}/base",
                files={"file": ("base.zip", b"x" * 4096, "application/zip")},
            )
        assert resp.status_code == 413
        assert resp.json()["detail"]["code"] == "UPLOAD_TOO_LARGE"
        s3_mock.upload_file.assert_not_awaited()

    async def test_version_collision_409(
        self,
        _wire: tuple[AsyncMock, AsyncMock],
        project_env: dict[str, uuid.UUID],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        doc_id = project_env["project_doc_id"]

        async def _raise(**_kwargs: object) -> None:
            raise IntegrityError("INSERT ...", {}, Exception("dup version"))

        monkeypatch.setattr(
            documents_module, "enqueue_base_normalize", AsyncMock(side_effect=_raise)
        )
        async with _client() as client:
            resp = await client.post(
                f"/api/v1/documents/{doc_id}/base",
                files={"file": ("base.zip", _zip_bytes(), "application/zip")},
            )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "BASE_VERSION_CONFLICT"
