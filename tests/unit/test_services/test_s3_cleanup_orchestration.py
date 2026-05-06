"""Tests for ``services.s3_cleanup_orchestration.enqueue_s3_cleanup``.

Phase 1 commit (i) ships the shared helper consumed by
``delete_document``, ``delete_node``, ``delete_file`` handlers (KD3
adoption — commits (k)/(l)/(m)) to schedule post-cascade S3 hard-
delete via :func:`s3_cleanup_task`. The helper bakes in the QQ5
ordering so each handler does NOT have to reimplement it three
times.

Tests verify the four observable contracts:

1. **Job row creation** — ``job_type="s3_cleanup"`` and
   ``input_params`` containing the supplied ``file_keys``.
2. **ARQ enqueue** — ``"s3_cleanup_task"`` invoked with the same
   ``file_keys`` and the ``Job.id`` (stringified) as ``job_id``.
3. **arq_job_id persistence** — second commit captures the ARQ-
   assigned id back onto the Job row.
4. **QQ5 ordering** — when ARQ enqueue is called, the Job row is
   ALREADY committed (visible from a fresh session). Without QQ5
   the ARQ task could fire before the Job is observable in DB.

The helper is exercised against a real DB (``requires_db``) since
the QQ5 commit sequencing is the central invariant; an in-memory
SQLAlchemy session would not show the cross-transaction visibility
that QQ5 guarantees. ARQ is mocked because the helper does not
care about real Redis — only the call shape matters.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from course_supporter.config import get_settings
from course_supporter.services.s3_cleanup_orchestration import enqueue_s3_cleanup
from course_supporter.storage.orm import Job, Tenant


@pytest.fixture(scope="module")
async def s3orch_engine() -> AsyncGenerator[AsyncEngine]:
    """Module-scoped engine for s3_cleanup_orchestration tests.

    Bound to the test database from settings — the QQ5 commit
    invariant is observable only across real transaction boundaries.
    """
    engine = create_async_engine(
        get_settings().database_url,
        pool_size=2,
        max_overflow=0,
    )
    yield engine
    await engine.dispose()


@pytest.fixture()
def s3orch_session_factory(
    s3orch_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        s3orch_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _make_tenant(session: AsyncSession, suffix: str) -> Tenant:
    tenant = Tenant(
        id=uuid.uuid4(),
        name=f"s3orch-{suffix}-{uuid.uuid4()}",
        is_active=True,
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_ids: list[uuid.UUID],
    job_ids: list[uuid.UUID],
) -> None:
    async with session_factory() as session:
        if job_ids:
            await session.execute(delete(Job).where(Job.id.in_(job_ids)))
        for tid in tenant_ids:
            await session.execute(delete(Tenant).where(Tenant.id == tid))
        await session.commit()


def _make_arq_mock(arq_job_id: str = "arq-test-job-id") -> AsyncMock:
    """Mock ARQ Redis client: returns a stub job with the given id."""
    arq = AsyncMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id=arq_job_id))
    return arq


@pytest.mark.requires_db
class TestEnqueueS3CleanupContract:
    """The four observable contracts of ``enqueue_s3_cleanup``."""

    async def test_creates_job_row_with_s3_cleanup_type(
        self,
        s3orch_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with s3orch_session_factory() as session:
            tenant = await _make_tenant(session, "create")
            await session.commit()
            tenant_id = tenant.id

        async with s3orch_session_factory() as session:
            arq = _make_arq_mock()
            file_keys = ["tenants/abc/files/1.md", "tenants/abc/files/2.md"]
            job = await enqueue_s3_cleanup(
                session=session,
                arq=arq,
                file_keys=file_keys,
                tenant_id=tenant_id,
            )
            job_id = job.id
            assert job.job_type == "s3_cleanup"

        # Verify persisted shape from a fresh session.
        async with s3orch_session_factory() as session:
            persisted = await session.get(Job, job_id)
            assert persisted is not None
            assert persisted.job_type == "s3_cleanup"
            assert persisted.input_params == {"file_keys": file_keys}
            assert persisted.tenant_id == tenant_id

        await _cleanup(s3orch_session_factory, [tenant_id], [job_id])

    async def test_arq_enqueue_call_shape(
        self,
        s3orch_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with s3orch_session_factory() as session:
            tenant = await _make_tenant(session, "arq-shape")
            await session.commit()
            tenant_id = tenant.id

        async with s3orch_session_factory() as session:
            arq = _make_arq_mock()
            file_keys = ["k1", "k2", "k3"]
            job = await enqueue_s3_cleanup(
                session=session,
                arq=arq,
                file_keys=file_keys,
                tenant_id=tenant_id,
            )
            job_id = job.id

        arq.enqueue_job.assert_awaited_once_with(
            "s3_cleanup_task",
            file_keys=file_keys,
            job_id=str(job_id),
        )

        await _cleanup(s3orch_session_factory, [tenant_id], [job_id])

    async def test_persists_arq_job_id_after_enqueue(
        self,
        s3orch_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with s3orch_session_factory() as session:
            tenant = await _make_tenant(session, "arq-id")
            await session.commit()
            tenant_id = tenant.id

        async with s3orch_session_factory() as session:
            arq = _make_arq_mock(arq_job_id="custom-arq-id-42")
            job = await enqueue_s3_cleanup(
                session=session,
                arq=arq,
                file_keys=["k"],
                tenant_id=tenant_id,
            )
            job_id = job.id

        async with s3orch_session_factory() as session:
            persisted = await session.get(Job, job_id)
            assert persisted is not None
            assert persisted.arq_job_id == "custom-arq-id-42"

        await _cleanup(s3orch_session_factory, [tenant_id], [job_id])

    async def test_arq_returns_none_leaves_arq_job_id_null(
        self,
        s3orch_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Rare: ARQ rejects enqueue (duplicate id, pool full) →
        ``enqueue_job`` returns ``None``. The Job row stays alive
        with ``arq_job_id=NULL``; reactivate-flow can re-issue.
        """
        async with s3orch_session_factory() as session:
            tenant = await _make_tenant(session, "arq-none")
            await session.commit()
            tenant_id = tenant.id

        async with s3orch_session_factory() as session:
            arq = AsyncMock()
            arq.enqueue_job = AsyncMock(return_value=None)
            job = await enqueue_s3_cleanup(
                session=session,
                arq=arq,
                file_keys=["k"],
                tenant_id=tenant_id,
            )
            job_id = job.id

        async with s3orch_session_factory() as session:
            persisted = await session.get(Job, job_id)
            assert persisted is not None
            assert persisted.arq_job_id is None
            # Reactivate-eligible: status is the post-create default
            # ("queued") which the dispatcher accepts after a
            # subsequent transition to "failed".
            assert persisted.job_type == "s3_cleanup"

        await _cleanup(s3orch_session_factory, [tenant_id], [job_id])


@pytest.mark.requires_db
class TestQQ5OrderingInvariant:
    """At the moment ``arq.enqueue_job`` is awaited, the Job row is
    ALREADY committed and visible from a fresh DB session. This is
    the QQ5 contract: DB → commit → ARQ enqueue.
    """

    async def test_job_row_visible_from_fresh_session_at_enqueue_time(
        self,
        s3orch_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with s3orch_session_factory() as session:
            tenant = await _make_tenant(session, "qq5")
            await session.commit()
            tenant_id = tenant.id

        observed_visible: dict[str, bool] = {"value": False}
        observed_job_id: dict[str, uuid.UUID | None] = {"value": None}

        async def probe_enqueue(*_args: Any, **kwargs: Any) -> Any:
            """Side-effect under enqueue: open a brand-new session
            and look up the Job by id. If QQ5 holds, the row is
            visible (committed before this side-effect ran).
            """
            job_id = uuid.UUID(kwargs["job_id"])
            observed_job_id["value"] = job_id
            async with s3orch_session_factory() as fresh:
                row = await fresh.get(Job, job_id)
                observed_visible["value"] = row is not None
            return MagicMock(job_id="arq-x")

        async with s3orch_session_factory() as session:
            arq = AsyncMock()
            arq.enqueue_job = AsyncMock(side_effect=probe_enqueue)
            job = await enqueue_s3_cleanup(
                session=session,
                arq=arq,
                file_keys=["k"],
                tenant_id=tenant_id,
            )
            job_id = job.id

        assert observed_visible["value"] is True, (
            "QQ5 violated: Job row not visible in fresh session at "
            "the moment ARQ enqueue fires"
        )
        assert observed_job_id["value"] == job_id

        await _cleanup(s3orch_session_factory, [tenant_id], [job_id])
