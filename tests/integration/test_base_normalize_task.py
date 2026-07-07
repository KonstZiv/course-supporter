"""Live-gesture tests for base_normalize_task (KD18 P2).

Real MinIO + real PostgreSQL: upload an archive, run the task directly with a
real ctx (no ARQ needed), assert the project_bases row transitions. BOTH arms:

* READY on a valid project zip — and snapshot_hash is the DETERMINISTIC
  aggregate over the NORMALIZED content (== manifest.aggregate_hash), NOT a
  hash of the raw zip bytes (point A — the base+delta echo-match axis).
* FAILED on a REAL malformed archive — the SecurityRejectedError → mark_failed
  path runs live (not mocked), with a category-prefixed reason.
"""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.config import get_settings
from course_supporter.jobs import JobType
from course_supporter.normalizer import normalize_archive
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from course_supporter.storage.s3 import S3Client
from course_supporter.workers.base_normalize import (
    _BASE_NORMALIZE_LIMITS,
    _snapshot_key_for,
    base_normalize_task,
)

pytestmark = pytest.mark.requires_db


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


def _valid_zip() -> bytes:
    """A small real project zip: two source files + a denylist directory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.py", "print('hello')\n")
        zf.writestr("README.md", "# Base project\n")
        zf.writestr(".venv/lib/site.bin", b"\x00\x01\x02\x03")
    return buf.getvalue()


async def _seed_base(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    doc_id: uuid.UUID,
    node_id: uuid.UUID,
    tenant_id: uuid.UUID,
    archive_key: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a pending ProjectBase + a queued base_normalize Job (committed)."""
    async with session_factory() as session:
        base = await ProjectBaseRepository(session).create_version(
            authored_document_id=doc_id, archive_key=archive_key
        )
        job = await JobRepository(session).create(
            tenant_id=tenant_id,
            course_node_id=node_id,
            job_type=JobType.BASE_NORMALIZE,
            input_params={"project_base_id": str(base.id)},
        )
        await session.commit()
        return base.id, job.id


def _archive_key(seeds: dict[str, uuid.UUID]) -> str:
    return (
        f"tenants/{seeds['tenant_id']}/nodes/{seeds['course_node_id']}/"
        f"bases/{seeds['material_id']}/v1/original.zip"
    )


class TestBaseNormalizeTaskLive:
    async def test_ready_on_valid_archive(
        self,
        s3_client: S3Client,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        raw = _valid_zip()
        archive_key = _archive_key(committed_seeds)
        snapshot_key = _snapshot_key_for(archive_key)
        await s3_client.upload_file(archive_key, raw, "application/zip")
        base_id, job_id = await _seed_base(
            session_factory,
            doc_id=committed_seeds["material_id"],
            node_id=committed_seeds["course_node_id"],
            tenant_id=committed_seeds["tenant_id"],
            archive_key=archive_key,
        )

        ctx = {"s3_client": s3_client, "session_factory": session_factory}
        result = await base_normalize_task(ctx, str(job_id), str(base_id))

        expected = normalize_archive(
            raw, archive_kind="zip", limits=_BASE_NORMALIZE_LIMITS
        )
        assert result["state"] == "ready"
        # Point A: the aggregate over NORMALIZED content, not the raw-zip hash.
        assert result["snapshot_hash"] == expected.snapshot_hash
        assert result["snapshot_hash"] != hashlib.sha256(raw).hexdigest()

        async with session_factory() as session:
            base = await ProjectBaseRepository(session).get_by_id(base_id)
            assert base is not None
            assert base.state == "ready"
            assert base.snapshot_hash == expected.snapshot_hash
            assert base.snapshot_key == snapshot_key
            assert base.manifest is not None
            assert base.manifest["schema"] == 1
            # The forgotten .venv collapsed into an excluded manifest row —
            # it did not fail the base.
            assert any(e["reason"] == "denylist_dir" for e in base.manifest["excluded"])
            assert base.failure_reason is None
            job = await JobRepository(session).get_by_id(job_id)
            assert job is not None
            assert job.status == "complete"

        # The canonical snapshot actually landed in S3, byte-reproducible.
        body = await s3_client.get_object(snapshot_key)
        assert body == expected.canonical_zip

        await s3_client.delete_object(archive_key)
        await s3_client.delete_object(snapshot_key)

    async def test_failed_on_malformed_archive(
        self,
        s3_client: S3Client,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        raw = b"this is definitely not a zip archive" * 64
        archive_key = _archive_key(committed_seeds)
        await s3_client.upload_file(archive_key, raw, "application/zip")
        base_id, job_id = await _seed_base(
            session_factory,
            doc_id=committed_seeds["material_id"],
            node_id=committed_seeds["course_node_id"],
            tenant_id=committed_seeds["tenant_id"],
            archive_key=archive_key,
        )

        ctx = {"s3_client": s3_client, "session_factory": session_factory}
        result = await base_normalize_task(ctx, str(job_id), str(base_id))

        assert result["state"] == "failed"
        async with session_factory() as session:
            base = await ProjectBaseRepository(session).get_by_id(base_id)
            assert base is not None
            assert base.state == "failed"
            # Category-prefixed reason (Decision 3): "<category>: <detail>".
            assert base.failure_reason is not None
            assert ":" in base.failure_reason
            assert base.snapshot_key is None
            assert base.snapshot_hash is None
            job = await JobRepository(session).get_by_id(job_id)
            assert job is not None
            assert job.status == "failed"

        await s3_client.delete_object(archive_key)
