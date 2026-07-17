"""Live gesture: s3_cleanup_task through the L2 seam (F5 close / acceptance §4).

Real MinIO + PostgreSQL. Seed a queued s3_cleanup Job (NULL subject, R2), upload
keys, run the wrapped task, and assert the seam drove the row to ``complete``
with the delete result on ``Job.result_data`` — the F5 fix (before L2 the
s3_cleanup row never moved out of ``queued``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.config import get_settings
from course_supporter.jobs import JobType
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.s3 import S3Client
from course_supporter.workers.s3_cleanup import s3_cleanup_task

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


async def _seed_cleanup_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    file_keys: list[str],
) -> uuid.UUID:
    """Create a queued s3_cleanup Job with a NULL subject (R2), committed."""
    async with session_factory() as session:
        job = await JobRepository(session).create(
            tenant_id=tenant_id,
            course_node_id=node_id,
            job_type=JobType.S3_CLEANUP,
            input_params={"file_keys": file_keys},
            subject_type=None,
            subject_id=None,
        )
        await session.commit()
        return job.id


class TestS3CleanupSeam:
    async def test_complete_with_result_data(
        self,
        s3_client: S3Client,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        prefix = f"tenants/{committed_seeds['tenant_id']}/cleanup"
        present = f"{prefix}/present.bin"
        missing = f"{prefix}/missing.bin"  # never uploaded → idempotent-deleted
        await s3_client.upload_file(present, b"payload", "application/octet-stream")

        job_id = await _seed_cleanup_job(
            session_factory,
            tenant_id=committed_seeds["tenant_id"],
            node_id=committed_seeds["course_node_id"],
            file_keys=[present, missing],
        )

        ctx = {"s3_client": s3_client, "session_factory": session_factory}
        # The seam owns the return: the wrapped task yields None; the delete
        # result lands on Job.result_data (NULL subject → liveness skipped).
        result = await s3_cleanup_task(ctx, str(job_id), file_keys=[present, missing])
        assert result is None

        async with session_factory() as session:
            job = await JobRepository(session).get_by_id(job_id)
            assert job is not None
            assert job.status == "complete"  # F5: the row now moves out of queued
            assert job.started_at is not None
            assert job.completed_at is not None
            assert job.result_data is not None
            # present deleted + missing counted deleted (idempotent), no errors.
            assert set(job.result_data["deleted"]) == {present, missing}
            assert job.result_data["errors"] == []

        # The present key is actually gone from S3.
        with pytest.raises(ClientError):
            await s3_client.get_object(present)
