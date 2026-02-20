"""Shared fixtures for integration tests requiring live infrastructure."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from course_supporter.config import get_settings
from course_supporter.storage.orm import Course, Job, SourceMaterial, Tenant

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
async def seed_course(db_session: AsyncSession, seed_tenant: Tenant) -> Course:
    """Create a Course row linked to seed_tenant."""
    course = Course(
        tenant_id=seed_tenant.id,
        title="Integration Test Course",
    )
    db_session.add(course)
    await db_session.flush()
    return course


@pytest.fixture()
async def seed_material(
    db_session: AsyncSession, seed_course: Course
) -> SourceMaterial:
    """Create a SourceMaterial in 'pending' status."""
    material = SourceMaterial(
        course_id=seed_course.id,
        source_type="web",
        source_url="https://example.com/test",
        status="pending",
    )
    db_session.add(material)
    await db_session.flush()
    return material


# ── Committed seeds (real commit + DELETE cleanup) ─────────────────


@pytest.fixture()
async def committed_seeds(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Create Tenant + Course + SourceMaterial with real commits.

    Returns dict with ``tenant_id``, ``course_id``, ``material_id``.
    Cleans up after the test via DELETE in reverse FK order.
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"test-tenant-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        course = Course(tenant_id=tenant.id, title="E2E Test Course")
        session.add(course)
        await session.flush()

        material = SourceMaterial(
            course_id=course.id,
            source_type="web",
            source_url="https://example.com/e2e",
            status="pending",
        )
        session.add(material)
        await session.flush()
        await session.commit()

        ids: dict[str, uuid.UUID] = {
            "tenant_id": tenant.id,
            "course_id": course.id,
            "material_id": material.id,
        }

    yield ids

    # Cleanup: delete in reverse FK order
    async with session_factory() as session:
        await session.execute(
            Job.__table__.delete().where(Job.course_id == ids["course_id"])
        )
        await session.execute(
            SourceMaterial.__table__.delete().where(
                SourceMaterial.course_id == ids["course_id"]
            )
        )
        await session.execute(
            Course.__table__.delete().where(Course.id == ids["course_id"])
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
    """Create a Job (active) + Material (processing) for callback tests.

    Pre-transitions the records to the state expected before
    ``IngestionCallback.on_success`` / ``on_failure``.

    Returns dict with ``job_id``, ``material_id``, ``course_id``, ``tenant_id``.
    """
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.repositories import SourceMaterialRepository

    async with session_factory() as session:
        job_repo = JobRepository(session)
        mat_repo = SourceMaterialRepository(session)

        job = await job_repo.create(
            course_id=committed_seeds["course_id"],
            job_type="ingest",
        )
        await job_repo.update_status(job.id, "active")
        await mat_repo.update_status(committed_seeds["material_id"], "processing")
        await session.commit()

    yield {
        "job_id": job.id,
        "material_id": committed_seeds["material_id"],
        "course_id": committed_seeds["course_id"],
        "tenant_id": committed_seeds["tenant_id"],
    }


# ── Redis fixture ─────────────────────────────────────────────────


@pytest.fixture()
async def arq_redis() -> AsyncGenerator[Any]:
    """Create and close a real ArqRedis connection pool."""
    from arq.connections import RedisSettings, create_pool

    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    yield pool
    await pool.aclose()
