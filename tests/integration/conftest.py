"""Shared fixtures for integration tests requiring live infrastructure."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arq.connections import ArqRedis

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from course_supporter.config import get_settings
from course_supporter.storage.orm import Job, MaterialEntry, MaterialNode, Tenant

# ── Engine (module-scoped, shared across test module) ──────────────


@pytest.fixture(scope="module")
async def async_engine() -> AsyncGenerator[AsyncEngine]:
    """Create an async engine from settings (module-scoped)."""
    engine = create_async_engine(
        get_settings().database_url,
        pool_size=5,
        max_overflow=0,
    )
    yield engine
    await engine.dispose()


# ── Session factory ────────────────────────────────────────────────


@pytest.fixture()
def session_factory(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the test engine."""
    return async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


# ── Session with savepoint rollback ───────────────────────────────


@pytest.fixture()
async def db_session(
    async_engine: AsyncEngine,
) -> AsyncGenerator[AsyncSession]:
    """Provide a session wrapped in a transaction, rolled back after test.

    Suitable for repository tests that use ``flush()`` but NOT ``commit()``.
    Tests that need ``commit()`` (e.g., IngestionCallback) should use
    ``session_factory`` + ``committed_seeds`` instead.
    """
    async with async_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)

        yield session

        await session.close()
        await trans.rollback()


# ── FK chain seed fixtures (savepoint) ────────────────────────────


@pytest.fixture()
async def seed_tenant(db_session: AsyncSession) -> Tenant:
    """Create a Tenant row for FK satisfaction."""
    tenant = Tenant(name=f"test-tenant-{uuid.uuid4().hex[:8]}")
    db_session.add(tenant)
    await db_session.flush()
    return tenant


@pytest.fixture()
async def seed_root_node(db_session: AsyncSession, seed_tenant: Tenant) -> MaterialNode:
    """Create a root MaterialNode linked to seed_tenant."""
    node = MaterialNode(
        tenant_id=seed_tenant.id,
        title="Integration Test Course",
        order=0,
    )
    db_session.add(node)
    await db_session.flush()
    return node


@pytest.fixture()
async def seed_material_entry(
    db_session: AsyncSession, seed_root_node: MaterialNode
) -> MaterialEntry:
    """Create a MaterialEntry in RAW state."""
    entry = MaterialEntry(
        materialnode_id=seed_root_node.id,
        source_type="web",
        source_url="https://example.com/test",
    )
    db_session.add(entry)
    await db_session.flush()
    return entry


# ── Committed seeds (real commit + DELETE cleanup) ─────────────────


@pytest.fixture()
async def committed_seeds(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Create Tenant + root MaterialNode + MaterialEntry with real commits.

    Returns dict with ``tenant_id``, ``materialnode_id``, ``material_id``.
    Cleans up after the test via DELETE in reverse FK order.
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"test-tenant-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        node = MaterialNode(
            tenant_id=tenant.id,
            title="E2E Test Course",
            order=0,
        )
        session.add(node)
        await session.flush()

        entry = MaterialEntry(
            materialnode_id=node.id,
            source_type="web",
            source_url="https://example.com/e2e",
        )
        session.add(entry)
        await session.flush()
        await session.commit()

        ids: dict[str, uuid.UUID] = {
            "tenant_id": tenant.id,
            "materialnode_id": node.id,
            "material_id": entry.id,
        }

    yield ids

    # Cleanup: delete in reverse FK order
    async with session_factory() as session:
        await session.execute(
            Job.__table__.delete().where(Job.materialnode_id == ids["materialnode_id"])
        )
        await session.execute(
            MaterialEntry.__table__.delete().where(
                MaterialEntry.materialnode_id == ids["materialnode_id"]
            )
        )
        await session.execute(
            MaterialNode.__table__.delete().where(
                MaterialNode.id == ids["materialnode_id"]
            )
        )
        await session.execute(
            Tenant.__table__.delete().where(Tenant.id == ids["tenant_id"])
        )
        await session.commit()


@pytest.fixture()
async def committed_job_and_material(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> AsyncGenerator[dict[str, Any]]:
    """Create a Job (active) + MaterialEntry for callback tests.

    Pre-transitions the records to the state expected before
    ``IngestionCallback.on_success`` / ``on_failure``.

    Returns dict with ``job_id``, ``material_id``, ``materialnode_id``, ``tenant_id``.
    """
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.material_entry_repository import (
        MaterialEntryRepository,
    )

    async with session_factory() as session:
        job_repo = JobRepository(session)
        entry_repo = MaterialEntryRepository(session)

        job = await job_repo.create(
            tenant_id=committed_seeds["tenant_id"],
            node_id=committed_seeds["materialnode_id"],
            job_type="ingest",
        )
        await job_repo.update_status(job.id, "active")
        await entry_repo.set_pending(committed_seeds["material_id"], job.id)
        await session.commit()

    yield {
        "job_id": job.id,
        "material_id": committed_seeds["material_id"],
        "materialnode_id": committed_seeds["materialnode_id"],
        "tenant_id": committed_seeds["tenant_id"],
    }


# ── Redis fixture ─────────────────────────────────────────────────


@pytest.fixture()
async def arq_redis() -> AsyncGenerator[ArqRedis]:
    """Create and close a real ArqRedis connection pool."""
    from arq.connections import RedisSettings, create_pool

    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    yield pool
    await pool.aclose()
