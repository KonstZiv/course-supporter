"""Real-DB tests for ProjectBaseRepository (KD18 P2).

Runs against real PostgreSQL (``requires_db``): version allocation leans on
``MAX(version) + 1`` + the ``UNIQUE(authored_document_id, version)`` arbiter and
the ``state`` CHECK — Postgres semantics an in-memory surrogate would not honour.

Covers, beyond happy-path: the get_latest vs get_latest_ready DIVERGENCE
(watch-item 2 — a pending latest over an earlier READY), the unique-version
collision arbiter (watch-item 1), the CHECK rejecting a bad state value, and the
append-only one-shot update-state pair (watch-item 3).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ProjectBase,
    Tenant,
)
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


@pytest.fixture(scope="module")
async def pb_engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(
        get_settings().database_url,
        pool_size=2,
        max_overflow=0,
    )
    yield engine
    await engine.dispose()


@pytest.fixture()
def pb_session_factory(
    pb_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pb_engine, class_=AsyncSession, expire_on_commit=False)


async def _make_project_doc(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create tenant + root node + a project AuthoredDocument. Committed."""
    async with factory() as session:
        tenant = Tenant(id=uuid.uuid4(), name=f"pb-{uuid.uuid4()}", is_active=True)
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(
            id=uuid.uuid4(), tenant_id=tenant.id, title="root", order=0
        )
        session.add(node)
        await session.flush()
        doc = AuthoredDocument(
            id=uuid.uuid4(),
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="s3://base-original",
            task_type="project",
        )
        session.add(doc)
        await session.commit()
        return doc.id, tenant.id


async def _cleanup(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Tear down the tenant's docs + nodes. Deleting authored_documents
    cascade-drops their project_bases (FK ON DELETE CASCADE)."""
    async with factory() as session:
        node_ids = select(CourseNode.id).where(CourseNode.tenant_id == tenant_id)
        await session.execute(
            delete(AuthoredDocument).where(
                AuthoredDocument.course_root_id.in_(node_ids)
            )
        )
        await session.execute(
            delete(CourseNode).where(CourseNode.tenant_id == tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


@pytest.fixture()
async def project_doc(
    pb_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], uuid.UUID]]:
    doc_id, tenant_id = await _make_project_doc(pb_session_factory)
    yield pb_session_factory, doc_id
    await _cleanup(pb_session_factory, tenant_id)


class TestCreateVersion:
    async def test_first_version_is_one_pending(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        factory, doc_id = project_doc
        async with factory() as session:
            base = await ProjectBaseRepository(session).create_version(
                authored_document_id=doc_id, archive_key="k/v1/original.zip"
            )
            assert base.version == 1
            assert base.state == "pending"
            assert base.archive_key == "k/v1/original.zip"
            assert base.snapshot_key is None
            assert base.snapshot_hash is None
            assert base.manifest is None
            assert base.failure_reason is None

    async def test_second_version_increments(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            v1 = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            v2 = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v2"
            )
            assert v1.version == 1
            assert v2.version == 2

    async def test_duplicate_version_hits_unique_arbiter(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        factory, doc_id = project_doc
        async with factory() as session:
            await ProjectBaseRepository(session).create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            # Force the concurrency collision: a second row with the SAME
            # (doc, version) — the UNIQUE index is the arbiter, not the read.
            session.add(
                ProjectBase(
                    authored_document_id=doc_id,
                    version=1,
                    archive_key="k/dup",
                    state="pending",
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()


class TestGetLatestDivergence:
    async def test_latest_vs_latest_ready_diverge(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        """READY v1 + pending v2: get_latest → v2, get_latest_ready → v1."""
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            v1 = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            await repo.mark_ready(
                v1.id,
                snapshot_key="k/v1/snapshot.zip",
                snapshot_hash="a" * 64,
                manifest={"schema": 1, "aggregate_hash": "a" * 64},
            )
            v2 = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v2"
            )

            latest = await repo.get_latest(doc_id)
            latest_ready = await repo.get_latest_ready(doc_id)
            assert latest is not None
            assert latest_ready is not None
            assert latest.id == v2.id
            assert latest.state == "pending"
            assert latest_ready.id == v1.id
            assert latest_ready.state == "ready"

    async def test_latest_ready_none_when_no_ready(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            v1 = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            assert await repo.get_latest_ready(doc_id) is None
            latest = await repo.get_latest(doc_id)
            assert latest is not None
            assert latest.id == v1.id

    async def test_both_none_when_no_base(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            assert await repo.get_latest(doc_id) is None
            assert await repo.get_latest_ready(doc_id) is None


class TestUpdateState:
    async def test_mark_ready_sets_snapshot_and_state(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            base = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            updated = await repo.mark_ready(
                base.id,
                snapshot_key="k/v1/snapshot.zip",
                snapshot_hash="b" * 64,
                manifest={"schema": 1, "included": []},
            )
            assert updated.state == "ready"
            assert updated.snapshot_key == "k/v1/snapshot.zip"
            assert updated.snapshot_hash == "b" * 64
            assert updated.manifest == {"schema": 1, "included": []}
            assert updated.failure_reason is None

    async def test_mark_failed_sets_reason_and_state(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            base = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            updated = await repo.mark_failed(
                base.id, failure_reason="malformed_archive: bad zip"
            )
            assert updated.state == "failed"
            assert updated.failure_reason == "malformed_archive: bad zip"

    async def test_state_check_rejects_invalid_value(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        factory, doc_id = project_doc
        async with factory() as session:
            session.add(
                ProjectBase(
                    authored_document_id=doc_id,
                    version=1,
                    archive_key="k/v1",
                    state="bogus",
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_mark_missing_raises(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        factory, _ = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            with pytest.raises(ValueError, match="ProjectBase not found"):
                await repo.mark_failed(uuid.uuid4(), failure_reason="x")


class TestFindBySnapshotHash:
    async def test_matches_any_ready_version(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        """Echo-match resolves the exact version, not just the latest — a
        student who built on v1 while v2 is active still resolves to v1."""
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            v1 = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            await repo.mark_ready(
                v1.id,
                snapshot_key="k/v1/snapshot.zip",
                snapshot_hash="1" * 64,
                manifest={"schema": 1},
            )
            v2 = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v2"
            )
            await repo.mark_ready(
                v2.id,
                snapshot_key="k/v2/snapshot.zip",
                snapshot_hash="2" * 64,
                manifest={"schema": 1},
            )

            hit_v1 = await repo.find_by_snapshot_hash(doc_id, "1" * 64)
            hit_v2 = await repo.find_by_snapshot_hash(doc_id, "2" * 64)
            assert hit_v1 is not None and hit_v1.id == v1.id and hit_v1.version == 1
            assert hit_v2 is not None and hit_v2.id == v2.id and hit_v2.version == 2

    async def test_unknown_hash_is_none(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            base = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            await repo.mark_ready(
                base.id,
                snapshot_key="k/v1/snapshot.zip",
                snapshot_hash="1" * 64,
                manifest={"schema": 1},
            )
            assert await repo.find_by_snapshot_hash(doc_id, "9" * 64) is None

    async def test_pending_and_failed_never_match(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        """snapshot_hash is NULL until READY — an echo can never resolve a
        pending / failed version (nothing to compare against)."""
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            pending = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            failed = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v2"
            )
            await repo.mark_failed(failed.id, failure_reason="bad zip")
            # Neither has a snapshot_hash → no echo can match; the (NULL == x)
            # comparison is never true in SQL either.
            assert pending.snapshot_hash is None
            assert await repo.find_by_snapshot_hash(doc_id, "1" * 64) is None

    async def test_scoped_to_document(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        """The same hash under a FOREIGN document does not match."""
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            base = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            await repo.mark_ready(
                base.id,
                snapshot_key="k/v1/snapshot.zip",
                snapshot_hash="1" * 64,
                manifest={"schema": 1},
            )
            other_doc = uuid.uuid4()
            assert await repo.find_by_snapshot_hash(other_doc, "1" * 64) is None

    async def test_identical_reupload_resolves_newest(
        self, project_doc: tuple[async_sessionmaker[AsyncSession], uuid.UUID]
    ) -> None:
        """Byte-identical re-upload (two versions share a hash) → newest match,
        deterministically (minimal staleness)."""
        factory, doc_id = project_doc
        async with factory() as session:
            repo = ProjectBaseRepository(session)
            v1 = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v1"
            )
            await repo.mark_ready(
                v1.id,
                snapshot_key="k/v1/snapshot.zip",
                snapshot_hash="7" * 64,
                manifest={"schema": 1},
            )
            v2 = await repo.create_version(
                authored_document_id=doc_id, archive_key="k/v2"
            )
            await repo.mark_ready(
                v2.id,
                snapshot_key="k/v2/snapshot.zip",
                snapshot_hash="7" * 64,
                manifest={"schema": 1},
            )
            hit = await repo.find_by_snapshot_hash(doc_id, "7" * 64)
            assert hit is not None and hit.id == v2.id and hit.version == 2
