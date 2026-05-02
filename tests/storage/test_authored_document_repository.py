"""Tests for AuthoredDocumentRepository.create() KD-delta defensive default.

Phase 1 commit (f) extends ``AuthoredDocumentRepository.create`` with
an optional ``course_root_id`` kwarg per vision §3 KD-delta. When the
caller supplies the value, the repository persists it directly (used
by ingestion pipeline that already has the root in scope). When the
caller omits it, the repository walks the parent chain via the
tenant-scoped variant of ``CourseNodeRepository.get_root_for``
(defense-in-depth per rule #12 — a malformed parent pointing at a
different tenant terminates the walk early and raises rather than
silently resolving to a foreign tenant's root).

Tests run against the real database (``requires_db`` marker) because
the resolution path is a recursive CTE walking ``parent_id`` over real
PostgreSQL — an in-memory SQLite surrogate would not honour the same
dialect or recursive CTE semantics.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from course_supporter.config import get_settings
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.orm import AuthoredDocument, CourseNode, Tenant


@pytest.fixture(scope="module")
async def kd_delta_engine() -> AsyncGenerator[AsyncEngine]:
    """Module-scoped engine for KD-delta defensive-default tests.

    Bound to the test database from settings — the parent-walk CTE
    requires real PostgreSQL recursive-CTE semantics that an
    in-memory SQLite surrogate would not honour.
    """
    engine = create_async_engine(
        get_settings().database_url,
        pool_size=2,
        max_overflow=0,
    )
    yield engine
    await engine.dispose()


@pytest.fixture()
def kd_delta_session_factory(
    kd_delta_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        kd_delta_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _make_tenant(session: AsyncSession, name_suffix: str) -> Tenant:
    tenant = Tenant(
        id=uuid.uuid4(),
        name=f"kd-delta-{name_suffix}-{uuid.uuid4()}",
        is_active=True,
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def _make_node(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    title: str,
) -> CourseNode:
    node = CourseNode(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        parent_id=parent_id,
        title=title,
        order=0,
    )
    session.add(node)
    await session.flush()
    return node


async def _cleanup_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_ids: list[uuid.UUID],
) -> None:
    """Tear down tenants + their authored_documents and course_nodes
    so each test leaves a clean slate. Order matters: documents first
    (FK cascade would handle it, but explicit deletion is robust under
    soft-delete-aware schemas)."""
    async with session_factory() as session:
        for tid in tenant_ids:
            await session.execute(
                delete(AuthoredDocument).where(
                    AuthoredDocument.course_root_id.in_(select_node_ids_for_tenant(tid))
                )
            )
            await session.execute(delete(CourseNode).where(CourseNode.tenant_id == tid))
            await session.execute(delete(Tenant).where(Tenant.id == tid))
        await session.commit()


def select_node_ids_for_tenant(tenant_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Subquery for cleanup — yields node ids for a given tenant."""
    from sqlalchemy import select

    return select(CourseNode.id).where(CourseNode.tenant_id == tenant_id)


@pytest.mark.requires_db
class TestCreateCourseRootIdDefensive:
    """``AuthoredDocumentRepository.create`` resolves ``course_root_id``
    via parent walk when not supplied (KD-delta default)."""

    async def test_create_with_explicit_course_root_id(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Caller-supplied value is persisted unchanged — repository
        must NOT override it via the parent walk.
        """
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "explicit")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            child = await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="child"
            )
            # Sentinel: pass an *unrelated* root id to prove the
            # repository honours the caller's value rather than
            # quietly recomputing.
            sentinel_root = uuid.uuid4()
            # Need this id to actually exist as a CourseNode for the
            # FK constraint — create a second tenant + root.
            other_tenant = await _make_tenant(session, "other")
            await _make_node(
                session,
                tenant_id=other_tenant.id,
                parent_id=None,
                title="other-root",
            )
            # Re-using the explicit id pattern: use other_tenant's root id.
            other_root = await _make_node(
                session,
                tenant_id=other_tenant.id,
                parent_id=None,
                title="explicit-target",
            )
            sentinel_root = other_root.id
            await session.commit()
            tenant_id, other_tenant_id = tenant.id, other_tenant.id
            child_id = child.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=child_id,
                source_type="text",
                source_url="https://example.com/explicit",
                course_root_id=sentinel_root,
            )
            await session.commit()
            assert doc.course_root_id == sentinel_root

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id, other_tenant_id])

    async def test_create_computes_root_when_not_provided(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Caller omits ``course_root_id`` → repository walks parent
        chain, persists the actual root id."""
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "compute")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            mid = await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="mid"
            )
            leaf = await _make_node(
                session, tenant_id=tenant.id, parent_id=mid.id, title="leaf"
            )
            await session.commit()
            root_id, leaf_id, tenant_id = root.id, leaf.id, tenant.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=leaf_id,
                source_type="text",
                source_url="https://example.com/computed",
            )
            await session.commit()
            assert doc.course_root_id == root_id

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])

    async def test_create_handles_root_node_input(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``node_id`` is itself the root (parent_id IS NULL) →
        ``course_root_id`` resolves to ``node_id``."""
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "rootinput")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root-only"
            )
            await session.commit()
            root_id, tenant_id = root.id, tenant.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=root_id,
                source_type="text",
                source_url="https://example.com/at-root",
            )
            await session.commit()
            assert doc.course_root_id == root_id

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])

    async def test_create_handles_deep_nesting(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """4-level tree (root → A → B → C); ``course_root_id`` from
        ``C`` walks all the way up."""
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "deep")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            a = await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="A"
            )
            b = await _make_node(
                session, tenant_id=tenant.id, parent_id=a.id, title="B"
            )
            c = await _make_node(
                session, tenant_id=tenant.id, parent_id=b.id, title="C"
            )
            await session.commit()
            root_id, c_id, tenant_id = root.id, c.id, tenant.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=c_id,
                source_type="text",
                source_url="https://example.com/deep",
            )
            await session.commit()
            assert doc.course_root_id == root_id

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])

    async def test_create_tenant_isolation_cross_tenant_parent_raises(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Defense-in-depth: when ``parent_id`` chain crosses tenants
        (data corruption scenario), the tenant-scoped walk terminates
        early and ``create`` raises rather than silently assigning a
        foreign tenant's root id (rule #12).
        """
        async with kd_delta_session_factory() as session:
            tenant_a = await _make_tenant(session, "tenant-a")
            tenant_b = await _make_tenant(session, "tenant-b")
            root_a = await _make_node(
                session, tenant_id=tenant_a.id, parent_id=None, title="root-a"
            )
            # Corrupt scenario: a node in tenant B points at tenant A's
            # root. Real-world this would be a bug or replication
            # artefact; the test asserts the defensive default refuses
            # to silently resolve.
            corrupted = await _make_node(
                session,
                tenant_id=tenant_b.id,
                parent_id=root_a.id,
                title="cross-tenant-child",
            )
            await session.commit()
            corrupted_id = corrupted.id
            tenant_ids = [tenant_a.id, tenant_b.id]

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            with pytest.raises(ValueError, match=r"Cannot resolve course_root_id"):
                await repo.create(
                    node_id=corrupted_id,
                    source_type="text",
                    source_url="https://example.com/cross-tenant",
                )

        await _cleanup_tenant(kd_delta_session_factory, tenant_ids)

    async def test_create_raises_when_node_id_does_not_exist(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Sanity guard: a non-existent ``node_id`` produces a clear
        error rather than a confusing FK constraint violation deeper
        in the call stack.
        """
        nonexistent = uuid.uuid4()
        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            with pytest.raises(ValueError, match=r"CourseNode not found"):
                await repo.create(
                    node_id=nonexistent,
                    source_type="text",
                    source_url="https://example.com/missing",
                )
