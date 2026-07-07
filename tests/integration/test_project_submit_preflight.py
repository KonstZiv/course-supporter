"""Route tests for the KD18 P3 project-submit preflight (both modes).

Full FastAPI request pipeline (auth + tenant + the real submit gates) against
real PostgreSQL; S3 and ARQ are dependency-overridden mocks (the full real-MinIO
+ real-ARQ end-to-end is the Commit-6 gesture). Covers the whole preflight
decision table via live HTTP on ``POST /homework/submit`` (mode-1) — every
reject is asserted to happen BEFORE the submission row is created (zero orphan) —
plus the byte-unchanged single-file / non-project regression and the task-aware
100 MB project cap.
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
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSummary,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


@pytest.fixture()
async def submit_env(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Tenant + root node + a READY project task + a READY non-project task."""
    async with session_factory() as session:
        tenant = Tenant(name=f"p3-submit-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="Submit", order=0)
        session.add(node)
        await session.flush()
        proj = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="s3://proj",
            task_type="project",
        )
        task = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="s3://task",
            task_type="task",
        )
        session.add_all([proj, task])
        await session.flush()
        for doc in (proj, task):
            session.add(
                DocumentSummary(
                    authored_document_id=doc.id,
                    course_root_id=node.id,
                    title="ready",
                    status="ready",
                )
            )
        await session.commit()
        ids = {
            "tenant_id": tenant.id,
            "node_id": node.id,
            "project_doc_id": proj.id,
            "task_doc_id": task.id,
        }
    yield ids
    async with session_factory() as session:
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == ids["tenant_id"]
            )
        )
        await session.execute(delete(Job).where(Job.tenant_id == ids["tenant_id"]))
        await session.execute(
            delete(Student).where(Student.tenant_id == ids["tenant_id"])
        )
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
    submit_env: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> Generator[AsyncMock]:
    s3_mock = AsyncMock()
    s3_mock.upload_smart = AsyncMock(return_value=("s3://bucket/stored.zip", 512))
    s3_mock.delete_object = AsyncMock()
    arq_mock = AsyncMock()
    arq_mock.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq:hw:1"))

    tenant = TenantContext(
        tenant_id=submit_env["tenant_id"],
        tenant_name="p3-submit",
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
    yield s3_mock
    app.dependency_overrides.clear()


async def _seed_base(
    session_factory: async_sessionmaker[AsyncSession],
    doc_id: uuid.UUID,
    *,
    ready: bool,
    snapshot_hash: str | None = None,
) -> uuid.UUID:
    """Create a base version; mark it READY with ``snapshot_hash`` if asked."""
    async with session_factory() as session:
        repo = ProjectBaseRepository(session)
        base = await repo.create_version(
            authored_document_id=doc_id, archive_key=f"k/{uuid.uuid4()}/original.zip"
        )
        if ready:
            await repo.mark_ready(
                base.id,
                snapshot_key="k/snapshot.zip",
                snapshot_hash=snapshot_hash or "0" * 64,
                manifest={"schema": 1},
            )
        await session.commit()
        return base.id


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.py", "print('hi')\n")
    return buf.getvalue()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _form(
    env: dict[str, uuid.UUID],
    doc_id: uuid.UUID,
    *,
    base_snapshot_hash: str | None = None,
) -> dict[str, str]:
    data = {
        "student_external_id": "ext-p3",
        "course_node_id": str(env["node_id"]),
        "node_id": str(env["node_id"]),
        "authored_document_id": str(doc_id),
    }
    if base_snapshot_hash is not None:
        data["base_snapshot_hash"] = base_snapshot_hash
    return data


async def _submission_count(
    session_factory: async_sessionmaker[AsyncSession], doc_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(HomeworkSubmission).where(
                        HomeworkSubmission.authored_document_id == doc_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


class TestPreflightRejects:
    """Every reject happens BEFORE the submission row + S3 upload (zero orphan)."""

    async def test_single_file_project_422_archive_only(
        self,
        _wire: AsyncMock,
        submit_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        s3_mock = _wire
        doc_id = submit_env["project_doc_id"]
        async with _client() as client:
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_form(submit_env, doc_id),
                files={"file": ("solution.py", b"x=1", "text/x-python")},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "ARCHIVE_ONLY"
        s3_mock.upload_smart.assert_not_awaited()
        assert await _submission_count(session_factory, doc_id) == 0

    async def test_base_not_ready_409(
        self,
        _wire: AsyncMock,
        submit_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        s3_mock = _wire
        doc_id = submit_env["project_doc_id"]
        await _seed_base(session_factory, doc_id, ready=False)
        async with _client() as client:
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_form(submit_env, doc_id),
                files={"file": ("proj.zip", _zip_bytes(), "application/zip")},
            )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "BASE_NOT_READY"
        s3_mock.upload_smart.assert_not_awaited()
        assert await _submission_count(session_factory, doc_id) == 0

    async def test_missing_echo_422(
        self,
        _wire: AsyncMock,
        submit_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        s3_mock = _wire
        doc_id = submit_env["project_doc_id"]
        await _seed_base(session_factory, doc_id, ready=True, snapshot_hash="a" * 64)
        async with _client() as client:
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_form(submit_env, doc_id),  # no base_snapshot_hash
                files={"file": ("proj.zip", _zip_bytes(), "application/zip")},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "MISSING_BASE_ECHO"
        s3_mock.upload_smart.assert_not_awaited()
        assert await _submission_count(session_factory, doc_id) == 0

    async def test_unknown_echo_422(
        self,
        _wire: AsyncMock,
        submit_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        s3_mock = _wire
        doc_id = submit_env["project_doc_id"]
        await _seed_base(session_factory, doc_id, ready=True, snapshot_hash="a" * 64)
        async with _client() as client:
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_form(submit_env, doc_id, base_snapshot_hash="b" * 64),
                files={"file": ("proj.zip", _zip_bytes(), "application/zip")},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "UNKNOWN_BASE_ECHO"
        s3_mock.upload_smart.assert_not_awaited()
        assert await _submission_count(session_factory, doc_id) == 0


class TestPreflightAccepts:
    async def test_no_base_creates_null_base_id(
        self,
        _wire: AsyncMock,
        submit_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        s3_mock = _wire
        doc_id = submit_env["project_doc_id"]
        async with _client() as client:
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_form(submit_env, doc_id),
                files={"file": ("proj.zip", _zip_bytes(), "application/zip")},
            )
        assert resp.status_code == 202, resp.text
        s3_mock.upload_smart.assert_awaited_once()
        async with session_factory() as session:
            sub = (
                await session.execute(
                    select(HomeworkSubmission).where(
                        HomeworkSubmission.authored_document_id == doc_id
                    )
                )
            ).scalar_one()
            assert sub.base_id is None

    async def test_valid_echo_creates_with_base_id(
        self,
        _wire: AsyncMock,
        submit_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        doc_id = submit_env["project_doc_id"]
        base_id = await _seed_base(
            session_factory, doc_id, ready=True, snapshot_hash="c" * 64
        )
        async with _client() as client:
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_form(submit_env, doc_id, base_snapshot_hash="c" * 64),
                files={"file": ("proj.zip", _zip_bytes(), "application/zip")},
            )
        assert resp.status_code == 202, resp.text
        async with session_factory() as session:
            sub = (
                await session.execute(
                    select(HomeworkSubmission).where(
                        HomeworkSubmission.authored_document_id == doc_id
                    )
                )
            ).scalar_one()
            assert sub.base_id == base_id

    async def test_older_version_echo_accepted(
        self,
        _wire: AsyncMock,
        submit_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Echo against an OLDER version is accepted (staleness ≠ error)."""
        doc_id = submit_env["project_doc_id"]
        v1_id = await _seed_base(
            session_factory, doc_id, ready=True, snapshot_hash="1" * 64
        )
        await _seed_base(session_factory, doc_id, ready=True, snapshot_hash="2" * 64)
        async with _client() as client:
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_form(submit_env, doc_id, base_snapshot_hash="1" * 64),
                files={"file": ("proj.zip", _zip_bytes(), "application/zip")},
            )
        assert resp.status_code == 202, resp.text
        async with session_factory() as session:
            sub = (
                await session.execute(
                    select(HomeworkSubmission).where(
                        HomeworkSubmission.authored_document_id == doc_id
                    )
                )
            ).scalar_one()
            assert sub.base_id == v1_id


class TestNonProjectUnchanged:
    async def test_single_file_non_project_202_no_base(
        self,
        _wire: AsyncMock,
        submit_env: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A non-project single-file submit is byte-unchanged: no preflight,
        base_id NULL, accepted with the ordinary 10 MB path."""
        doc_id = submit_env["task_doc_id"]
        async with _client() as client:
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_form(submit_env, doc_id),
                files={"file": ("solution.py", b"print(1)", "text/x-python")},
            )
        assert resp.status_code == 202, resp.text
        async with session_factory() as session:
            sub = (
                await session.execute(
                    select(HomeworkSubmission).where(
                        HomeworkSubmission.authored_document_id == doc_id
                    )
                )
            ).scalar_one()
            assert sub.base_id is None


class TestTaskAwareCap:
    async def test_project_large_upload_accepted(
        self,
        _wire: AsyncMock,
        submit_env: dict[str, uuid.UUID],
    ) -> None:
        """An 11 MB project upload passes (100 MB cap), not cut at 10 MB."""
        s3_mock = _wire
        s3_mock.upload_smart = AsyncMock(
            return_value=("s3://bucket/big.zip", 11 * 1024 * 1024)
        )
        doc_id = submit_env["project_doc_id"]
        async with _client() as client:
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_form(submit_env, doc_id),
                files={"file": ("big.zip", _zip_bytes(), "application/zip")},
            )
        assert resp.status_code == 202, resp.text

    async def test_non_project_large_upload_rejected(
        self,
        _wire: AsyncMock,
        submit_env: dict[str, uuid.UUID],
    ) -> None:
        """The same 11 MB upload on a non-project task is rejected at 10 MB —
        proving the cap is task-aware."""
        s3_mock = _wire
        s3_mock.upload_smart = AsyncMock(
            return_value=("s3://bucket/big.py", 11 * 1024 * 1024)
        )
        s3_mock.delete_object = AsyncMock()
        doc_id = submit_env["task_doc_id"]
        async with _client() as client:
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_form(submit_env, doc_id),
                files={"file": ("big.py", b"x=1", "text/x-python")},
            )
        assert resp.status_code == 422
        assert "too large" in resp.json()["detail"].lower()
        s3_mock.delete_object.assert_awaited_once()
