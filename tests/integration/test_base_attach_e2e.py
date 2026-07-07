"""Genuine end-to-end for the KD18 P2 base shell (real MinIO + real ARQ + DB).

Zero mocks on the critical path: the base archive is uploaded through the real
HTTP route to real MinIO, enqueued to real Redis, and normalized by a REAL arq
burst worker (real Redis dispatch → pickup → execute), then read back through the
real GET routes. Only the auth identity is injected (dependency override) — every
storage / queue / normalization hop is real.

The load-bearing assertion is the echo round-trip: the ``snapshot_hash`` that
``GET /base`` returns equals the deterministic aggregate the normalizer computes
over the same content — the contract P3's echo-match depends on.

Requires ``docker compose up -d`` (PostgreSQL + MinIO + Redis).
"""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name
from arq.worker import Worker
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.config import get_settings
from course_supporter.normalizer import normalize_archive
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import AuthoredDocument, CourseNode, Job, Tenant
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from course_supporter.storage.s3 import S3Client
from course_supporter.workers.base_normalize import (
    _BASE_NORMALIZE_LIMITS,
    base_normalize_task,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = [pytest.mark.requires_db, pytest.mark.requires_redis]


@pytest.fixture()
async def s3_client() -> AsyncGenerator[S3Client]:
    s = get_settings()
    client = S3Client(
        endpoint_url=s.s3_endpoint,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key.get_secret_value(),
        bucket=s.s3_bucket,
    )
    await client.open()
    try:
        await client.ensure_bucket()
        yield client
    finally:
        await client.close()


@pytest.fixture()
async def project_doc(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    async with session_factory() as session:
        tenant = Tenant(name=f"pb-e2e-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="E2E", order=0)
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
    project_doc: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
) -> Generator[None]:
    """Inject the auth identity + REAL infra (S3 / Redis / DB session).

    The overrides carry real objects (not mocks) — lifespan does not run under
    ASGITransport, so the pools are provided explicitly rather than stubbed.
    """
    tenant = TenantContext(
        tenant_id=project_doc["tenant_id"],
        tenant_name="pb-e2e",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_test",
    )

    async def _override_session() -> Any:
        async with session_factory() as session:
            yield session

    async def _override_tenant() -> TenantContext:
        return tenant

    async def _override_s3() -> S3Client:
        return s3_client

    async def _override_arq() -> ArqRedis:
        return arq_redis

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_tenant] = _override_tenant
    app.dependency_overrides[get_s3_client] = _override_s3
    app.dependency_overrides[get_arq_redis] = _override_arq
    yield
    app.dependency_overrides.clear()


def _project_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/main.py", "def run():\n    return 42\n")
        zf.writestr("README.md", "# Starter\n")
        zf.writestr(".git/config", "[core]\n")  # denylist-collapsed
    return buf.getvalue()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drain_base_normalize(
    session_factory: async_sessionmaker[AsyncSession], s3_client: S3Client
) -> None:
    """Run a REAL arq burst worker that drains the base_normalize job."""
    worker = Worker(
        functions=[base_normalize_task],
        redis_settings=RedisSettings.from_dsn(get_settings().redis_url),
        ctx={"s3_client": s3_client, "session_factory": session_factory},
        burst=True,
        handle_signals=False,
        poll_delay=0.05,
        max_jobs=1,
    )
    try:
        await worker.async_run()
    finally:
        await worker.close()


class TestBaseAttachE2E:
    async def test_attach_normalize_read_roundtrip(
        self,
        _wire: None,
        project_doc: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
        arq_redis: ArqRedis,
    ) -> None:
        doc_id = project_doc["doc_id"]
        raw = _project_zip()
        # Isolate the burst worker to just-this job.
        await arq_redis.delete(default_queue_name)

        async with _client() as client:
            attach = await client.post(
                f"/api/v1/documents/{doc_id}/base",
                files={"file": ("starter.zip", raw, "application/zip")},
            )
        assert attach.status_code == 202, attach.text
        base_id = uuid.UUID(attach.json()["base_version_id"])

        # The raw archive really landed in MinIO; the version is pending.
        async with session_factory() as session:
            base_row = await ProjectBaseRepository(session).get_by_id(base_id)
            assert base_row is not None
            archive_key = base_row.archive_key
            assert base_row.state == "pending"
        stored = await s3_client.get_object(archive_key)
        assert stored == raw

        # Real ARQ: burst worker drains the enqueued job → normalize for real.
        await _drain_base_normalize(session_factory, s3_client)

        async with _client() as client:
            base_resp = await client.get(f"/api/v1/homework/tasks/{doc_id}/base")
            manifest_resp = await client.get(
                f"/api/v1/documents/{doc_id}/base/manifest"
            )

        assert base_resp.status_code == 200, base_resp.text
        desc = base_resp.json()
        assert desc["state"] == "ready"
        assert desc["version"] == 1
        assert desc["original_url"]  # presigned GET of the original

        # Echo round-trip: the descriptor hash == the deterministic aggregate.
        expected = normalize_archive(
            raw, archive_kind="zip", limits=_BASE_NORMALIZE_LIMITS
        )
        assert desc["snapshot_hash"] == expected.snapshot_hash

        assert manifest_resp.status_code == 200, manifest_resp.text
        manifest = manifest_resp.json()
        assert manifest["schema"] == 1
        assert manifest["aggregate_hash"] == expected.snapshot_hash
        assert any(e["reason"] == "denylist_dir" for e in manifest["excluded"])

        # Snapshot really landed in MinIO; clean up both objects.
        async with session_factory() as session:
            final = await ProjectBaseRepository(session).get_by_id(base_id)
            assert final is not None
            assert final.snapshot_key is not None
            snapshot_body = await s3_client.get_object(final.snapshot_key)
            assert snapshot_body == expected.canonical_zip
            await s3_client.delete_object(archive_key)
            await s3_client.delete_object(final.snapshot_key)
