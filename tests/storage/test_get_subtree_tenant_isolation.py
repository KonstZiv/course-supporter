"""Tests for ``CourseNodeRepository.get_subtree`` tenant isolation.

Phase 1 commit (j) closes Gap 1: extends ``get_subtree`` with an
optional ``tenant_id`` kwarg and applies the filter to BOTH the
base anchor AND the recursive ``JOIN`` of the CTE. Without this
defense-in-depth, a malformed ``parent_id`` chain crossing tenants
(real-world: replication artefact or bug) would silently expose a
foreign tenant's subtree to the caller's session.

The fix mirrors the shape of:

* :meth:`CourseNodeRepository.get_descendant_ids` (shipped in 0.4)
* :meth:`CourseNodeRepository.get_root_for` (shipped in Phase 1
  commit (f) for KD-δ defensive default)

Tests run against the real database (``requires_db`` marker)
because the resolution path is a recursive CTE walking
``parent_id`` over real PostgreSQL — an in-memory SQLite surrogate
would not honour the same dialect or recursive CTE semantics.
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
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.orm import CourseNode, Tenant
from tests._helpers.course_node_factory import make_root_course_node


@pytest.fixture(scope="module")
async def gap1_engine() -> AsyncGenerator[AsyncEngine]:
    """Module-scoped engine for Gap 1 isolation tests.

    Bound to the test database from settings — recursive CTE
    semantics (``UNION ALL`` traversal with WHERE applied to BOTH
    anchor and recursive arm) are PostgreSQL-specific.
    """
    engine = create_async_engine(
        get_settings().database_url,
        pool_size=2,
        max_overflow=0,
    )
    yield engine
    await engine.dispose()


@pytest.fixture()
def gap1_session_factory(
    gap1_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        gap1_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _make_tenant(session: AsyncSession, suffix: str) -> Tenant:
    tenant = Tenant(
        id=uuid.uuid4(),
        name=f"gap1-{suffix}-{uuid.uuid4()}",
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
    # Task 2.4.13B — root branch routes through the factory so the
    # CHECK-satisfying ``default_language`` lands here. Child branch
    # stays raw (task 2.4.13 рішення 1 — child language is dead data).
    if parent_id is None:
        node = make_root_course_node(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            title=title,
            order=0,
        )
    else:
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
    async with session_factory() as session:
        for tid in tenant_ids:
            await session.execute(delete(CourseNode).where(CourseNode.tenant_id == tid))
            await session.execute(delete(Tenant).where(Tenant.id == tid))
        await session.commit()


@pytest.mark.requires_db
class TestGetSubtreeTenantIsolation:
    """``get_subtree`` with ``tenant_id`` kwarg honours tenant scope on
    both the base anchor and the recursive walk.
    """

    async def test_no_tenant_id_preserves_legacy_cross_tenant_walk(
        self,
        gap1_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Default ``tenant_id=None`` keeps the pre-Gap-1 behaviour:
        the CTE finds the node by id alone. Locks back-compat for
        callsites that already enforce isolation upstream (they do not
        need to opt in to the new kwarg in lockstep).
        """
        async with gap1_session_factory() as session:
            tenant = await _make_tenant(session, "default-walk")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            child = await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="child"
            )
            await session.commit()
            tenant_id, root_id, child_id = tenant.id, root.id, child.id

        async with gap1_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree(root_id)

        assert len(tree) == 1
        returned_root = tree[0]
        assert returned_root.id == root_id
        assert len(returned_root.children) == 1
        assert returned_root.children[0].id == child_id

        await _cleanup_tenant(gap1_session_factory, [tenant_id])

    async def test_matching_tenant_returns_subtree(
        self,
        gap1_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Caller's ``tenant_id`` matches the node's tenant — the
        recursion proceeds normally and the full subtree is returned.
        Locks the positive path: the dual-side filter is invisible
        when caller and data agree.
        """
        async with gap1_session_factory() as session:
            tenant = await _make_tenant(session, "matching")
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
            tenant_id, root_id = tenant.id, root.id
            mid_id, leaf_id = mid.id, leaf.id

        async with gap1_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree(root_id, tenant_id=tenant_id)

        assert len(tree) == 1
        ids_visited: set[uuid.UUID] = set()

        def _walk(node: CourseNode) -> None:
            ids_visited.add(node.id)
            for child in node.children:
                _walk(child)

        _walk(tree[0])
        assert ids_visited == {root_id, mid_id, leaf_id}

        await _cleanup_tenant(gap1_session_factory, [tenant_id])

    async def test_cross_tenant_root_id_returns_empty(
        self,
        gap1_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Caller passes a valid ``root_id`` from tenant A while
        querying as tenant B — the base-anchor filter rejects it and
        the CTE returns zero rows. This is the canonical Gap 1 lock:
        cross-tenant data invisible even with a known-valid id.
        """
        async with gap1_session_factory() as session:
            tenant_a = await _make_tenant(session, "alpha")
            tenant_b = await _make_tenant(session, "bravo")
            root_a = await _make_node(
                session,
                tenant_id=tenant_a.id,
                parent_id=None,
                title="alpha-root",
            )
            await _make_node(
                session,
                tenant_id=tenant_a.id,
                parent_id=root_a.id,
                title="alpha-child",
            )
            await session.commit()
            tenant_a_id, tenant_b_id = tenant_a.id, tenant_b.id
            root_a_id = root_a.id

        async with gap1_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree(root_a_id, tenant_id=tenant_b_id)

        assert tree == []

        await _cleanup_tenant(gap1_session_factory, [tenant_a_id, tenant_b_id])

    async def test_cross_tenant_parent_chain_terminates_walk(
        self,
        gap1_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Defense-in-depth: a corrupted ``parent_id`` chain that
        crosses tenants (real-world: replication artefact or bug) is
        terminated by the recursive-step filter. Walking from tenant
        A's root must NOT pull in tenant B's children even when a
        tenant B node points at a tenant A node as its parent.
        """
        async with gap1_session_factory() as session:
            tenant_a = await _make_tenant(session, "isolation-a")
            tenant_b = await _make_tenant(session, "isolation-b")
            root_a = await _make_node(
                session,
                tenant_id=tenant_a.id,
                parent_id=None,
                title="root-a",
            )
            child_a = await _make_node(
                session,
                tenant_id=tenant_a.id,
                parent_id=root_a.id,
                title="child-a",
            )
            # Corrupted: tenant B node pointing at tenant A's root.
            # Real-world this is a bug or replication artefact; the
            # test asserts the recursive-arm filter blocks it.
            await _make_node(
                session,
                tenant_id=tenant_b.id,
                parent_id=root_a.id,
                title="cross-tenant-leak",
            )
            await session.commit()
            tenant_a_id, tenant_b_id = tenant_a.id, tenant_b.id
            root_a_id, child_a_id = root_a.id, child_a.id

        async with gap1_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree(root_a_id, tenant_id=tenant_a_id)

        ids_visited: set[uuid.UUID] = set()

        def _walk(node: CourseNode) -> None:
            ids_visited.add(node.id)
            for child in node.children:
                _walk(child)

        for root in tree:
            _walk(root)

        # Only tenant A's nodes appear; the cross-tenant leak node is
        # filtered out by the recursive-step ``tenant_id`` filter.
        assert ids_visited == {root_a_id, child_a_id}

        await _cleanup_tenant(gap1_session_factory, [tenant_a_id, tenant_b_id])

    async def test_include_documents_compatible_with_tenant_filter(
        self,
        gap1_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``include_documents`` and ``tenant_id`` compose without
        interference — the tenant filter scopes the CTE membership,
        the eager-load option fans out documents per node. Locks
        that the new kwarg does not regress the documents pre-load
        contract used by ``delete_node`` (commit (k)).
        """
        async with gap1_session_factory() as session:
            tenant = await _make_tenant(session, "with-docs")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="child"
            )
            await session.commit()
            tenant_id, root_id = tenant.id, root.id

        async with gap1_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree(
                root_id, include_documents=True, tenant_id=tenant_id
            )

        assert len(tree) == 1
        # ``documents`` is eager-loaded — accessing it must NOT trigger
        # an implicit IO (which would raise after the session closes).
        for node in tree:
            _ = list(node.documents)
            for child in node.children:
                _ = list(child.documents)

        await _cleanup_tenant(gap1_session_factory, [tenant_id])
