"""Tests for ``CourseNodeRepository.get_subtree_with_active_documents`` and
the ``_node_response`` inline filter introduced by hotfix-7.

Hotfix-7 closes regression H from CHECKPOINT 2 Step 6: the user-facing
``GET /api/v1/nodes/{id}/detail`` endpoint exposed soft-deleted
``AuthoredDocument`` rows in the ``authored_documents`` array because
``CourseNodeRepository.get_subtree(include_documents=True)`` did not
filter ``AuthoredDocument.deleted_at IS NULL`` at the relationship
loader.

The fix introduces a new helper ``get_subtree_with_active_documents``
that wraps the same recursive CTE walk + tree assembly as
``get_subtree`` but pins the loader option to active rows only. The
``CourseNode.documents`` relationship itself stays unchanged so
internal consumers (cascade engine, fingerprint computation,
generation orchestrator, readiness, tree_utils, background tasks) keep
their unfiltered access.

Tests run against the real database (``requires_db`` marker) because
the loader-option filter is applied at SQL level via
``selectinload(CourseNode.documents.and_(...))`` — an in-memory
surrogate would not faithfully exercise the eager-load + filter
composition that PostgreSQL's planner resolves.

Two materialisation paths are exercised independently per Amendment 33
(enumerate-all-paths discipline):

1. The new ``get_subtree_with_active_documents`` helper itself —
   asserts the eager-loaded ``documents`` collection contains only
   ``deleted_at IS NULL`` rows.
2. The ``_node_response`` inline filter — asserts the
   ``authored_documents_count`` field on ``NodeResponse`` excludes
   soft-deleted rows. This filter lives in the route layer, not in
   the repository, so it has its own regression surface.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from course_supporter.api.routes.nodes import _node_response
from course_supporter.api.schemas import NodeResponse
from course_supporter.config import get_settings
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    Tenant,
)


@pytest.fixture(scope="module")
async def hotfix7_engine() -> AsyncGenerator[AsyncEngine]:
    """Module-scoped engine for hotfix-7 active-documents tests."""
    engine = create_async_engine(
        get_settings().database_url,
        pool_size=2,
        max_overflow=0,
    )
    yield engine
    await engine.dispose()


@pytest.fixture()
def hotfix7_session_factory(
    hotfix7_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        hotfix7_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _make_tenant(session: AsyncSession, suffix: str) -> Tenant:
    tenant = Tenant(
        id=uuid.uuid4(),
        name=f"hotfix7-{suffix}-{uuid.uuid4()}",
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


async def _make_document(
    session: AsyncSession,
    *,
    course_node_id: uuid.UUID,
    course_root_id: uuid.UUID,
    filename: str,
    deleted: bool = False,
) -> AuthoredDocument:
    doc = AuthoredDocument(
        id=uuid.uuid4(),
        course_node_id=course_node_id,
        course_root_id=course_root_id,
        source_type="text",
        source_url=f"s3://test/{filename}",
        filename=filename,
        order=0,
    )
    if deleted:
        doc.deleted_at = datetime.now(UTC)
    session.add(doc)
    await session.flush()
    return doc


async def _cleanup_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_ids: list[uuid.UUID],
) -> None:
    """Tear down tenants + their authored_documents and course_nodes."""
    from sqlalchemy import select as sa_select

    async with session_factory() as session:
        for tid in tenant_ids:
            node_id_subq = sa_select(CourseNode.id).where(CourseNode.tenant_id == tid)
            await session.execute(
                delete(AuthoredDocument).where(
                    AuthoredDocument.course_node_id.in_(node_id_subq)
                )
            )
            await session.execute(delete(CourseNode).where(CourseNode.tenant_id == tid))
            await session.execute(delete(Tenant).where(Tenant.id == tid))
        await session.commit()


@pytest.mark.requires_db
class TestGetSubtreeWithActiveDocuments:
    """``get_subtree_with_active_documents`` filters soft-deleted docs."""

    async def test_filters_soft_deleted_documents(
        self,
        hotfix7_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Helper returns subtree with only ``deleted_at IS NULL``
        documents on each node — the canonical regression-H lock.
        """
        async with hotfix7_session_factory() as session:
            tenant = await _make_tenant(session, "filter")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            active_doc = await _make_document(
                session,
                course_node_id=root.id,
                course_root_id=root.id,
                filename="active.txt",
                deleted=False,
            )
            await _make_document(
                session,
                course_node_id=root.id,
                course_root_id=root.id,
                filename="deleted.txt",
                deleted=True,
            )
            await session.commit()
            tenant_id, root_id, active_id = tenant.id, root.id, active_doc.id

        async with hotfix7_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree_with_active_documents(root_id)

        assert len(tree) == 1
        loaded_root = tree[0]
        assert loaded_root.id == root_id
        loaded_doc_ids = {d.id for d in loaded_root.documents}
        assert loaded_doc_ids == {active_id}

        await _cleanup_tenant(hotfix7_session_factory, [tenant_id])

    async def test_compose_with_tenant_filter(
        self,
        hotfix7_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Helper composes with ``tenant_id`` (Gap 1 dual-side filter)
        without regression — cross-tenant ``root_id`` returns empty
        even when active documents exist on it.
        """
        async with hotfix7_session_factory() as session:
            tenant_a = await _make_tenant(session, "compose-a")
            tenant_b = await _make_tenant(session, "compose-b")
            root_a = await _make_node(
                session,
                tenant_id=tenant_a.id,
                parent_id=None,
                title="root-a",
            )
            await _make_document(
                session,
                course_node_id=root_a.id,
                course_root_id=root_a.id,
                filename="a-active.txt",
                deleted=False,
            )
            await session.commit()
            tenant_a_id, tenant_b_id = tenant_a.id, tenant_b.id
            root_a_id = root_a.id

        async with hotfix7_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree_with_active_documents(
                root_a_id, tenant_id=tenant_b_id
            )

        assert tree == []

        await _cleanup_tenant(hotfix7_session_factory, [tenant_a_id, tenant_b_id])

    async def test_subtree_descendants_filter_their_documents(
        self,
        hotfix7_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Filter applies to every node in the subtree, not only root —
        a child node with mixed active/deleted documents loads only
        active ones.
        """
        async with hotfix7_session_factory() as session:
            tenant = await _make_tenant(session, "descendants")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            child = await _make_node(
                session,
                tenant_id=tenant.id,
                parent_id=root.id,
                title="child",
            )
            child_active = await _make_document(
                session,
                course_node_id=child.id,
                course_root_id=root.id,
                filename="child-active.txt",
                deleted=False,
            )
            await _make_document(
                session,
                course_node_id=child.id,
                course_root_id=root.id,
                filename="child-deleted.txt",
                deleted=True,
            )
            await session.commit()
            tenant_id, root_id = tenant.id, root.id
            child_id, child_active_id = child.id, child_active.id

        async with hotfix7_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree_with_active_documents(root_id)

        assert len(tree) == 1
        loaded_root = tree[0]
        assert len(loaded_root.children) == 1
        loaded_child = loaded_root.children[0]
        assert loaded_child.id == child_id
        loaded_doc_ids = {d.id for d in loaded_child.documents}
        assert loaded_doc_ids == {child_active_id}

        await _cleanup_tenant(hotfix7_session_factory, [tenant_id])


@pytest.mark.requires_db
class TestNodeResponseAuthoredDocumentsCountFilter:
    """``_node_response`` inline filter on ``authored_documents_count``.

    Independent path from the helper above — the count is computed in
    the route layer, not at the loader, so soft-deleted rows that
    arrive via any non-helper path (e.g. ``list_roots``'s unfiltered
    eager-load) must still be excluded from the user-facing count.
    """

    async def test_count_excludes_soft_deleted(
        self,
        hotfix7_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``authored_documents_count`` reflects only active documents
        even when ``node.documents`` carries soft-deleted rows.
        """
        async with hotfix7_session_factory() as session:
            tenant = await _make_tenant(session, "count")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            await _make_document(
                session,
                course_node_id=root.id,
                course_root_id=root.id,
                filename="active1.txt",
                deleted=False,
            )
            await _make_document(
                session,
                course_node_id=root.id,
                course_root_id=root.id,
                filename="active2.txt",
                deleted=False,
            )
            await _make_document(
                session,
                course_node_id=root.id,
                course_root_id=root.id,
                filename="deleted.txt",
                deleted=True,
            )
            await session.commit()
            tenant_id, root_id = tenant.id, root.id

        # Load the node via the legacy unfiltered path (mirrors what
        # ``list_roots`` returns) — soft-deleted rows are present on
        # ``node.documents``.
        async with hotfix7_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree(root_id, include_documents=True)
            assert len(tree) == 1
            loaded_root = tree[0]
            # Sanity check: the unfiltered loader carries all 3 rows.
            assert len(loaded_root.documents) == 3

            response = _node_response(loaded_root)

        assert isinstance(response, NodeResponse)
        # Inline filter excludes the soft-deleted row from the count.
        assert response.authored_documents_count == 2

        await _cleanup_tenant(hotfix7_session_factory, [tenant_id])


@pytest.mark.requires_db
class TestGetSubtreeWithActiveDocumentsFiltersDeletedNodes:
    """Hotfix-8: ``get_subtree_with_active_documents`` filters soft-
    deleted ``CourseNode`` rows from the recursive CTE walk.

    Regression M (caught at CHECKPOINT 2 Step 8): a cascade-deleted
    Module/Topic chain leaked into the subtree through ``node.children``
    because the recursive CTE walk did not constrain
    ``CourseNode.deleted_at IS NULL``. Hotfix-7 only filtered
    ``AuthoredDocument`` at the loader-option level; the second leak
    axis (``CourseNode`` traversal) was uncovered by the same kind of
    UI exposure (deleted nodes rendered with the KD3 marker as title).

    The fix mirrors the dual-side discipline already used for the Gap 1
    ``tenant_id`` filter — base anchor AND recursive arm both apply
    ``deleted_at IS NULL``. This independent path deserves its own
    fixture per Amendment 33: the relationship loader filter (hotfix-7)
    and the CTE walk filter (hotfix-8) sit at distinct points in the
    SQL composition and have distinct regression surfaces.
    """

    async def test_sibling_deleted_child_excluded(
        self,
        hotfix7_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Root has two children — one active, one soft-deleted. The
        helper returns the root with only the active child in
        ``children``.
        """
        async with hotfix7_session_factory() as session:
            tenant = await _make_tenant(session, "node-filter-sibling")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            active_child = await _make_node(
                session,
                tenant_id=tenant.id,
                parent_id=root.id,
                title="active-child",
            )
            deleted_child = await _make_node(
                session,
                tenant_id=tenant.id,
                parent_id=root.id,
                title="to-be-deleted",
            )
            deleted_child.deleted_at = datetime.now(UTC)
            await session.commit()
            tenant_id, root_id, active_child_id = (
                tenant.id,
                root.id,
                active_child.id,
            )

        async with hotfix7_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree_with_active_documents(root_id)

        assert len(tree) == 1
        loaded_root = tree[0]
        assert loaded_root.id == root_id
        loaded_child_ids = {c.id for c in loaded_root.children}
        assert loaded_child_ids == {active_child_id}

        await _cleanup_tenant(hotfix7_session_factory, [tenant_id])

    async def test_cascade_deleted_descendant_chain_excluded(
        self,
        hotfix7_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Root → Module → Topic chain with both Module and Topic
        soft-deleted (e.g. cascade soft-delete from Module). The
        recursive CTE walk terminates at the active root — neither
        Module nor Topic appears in ``children``. This is the canonical
        regression-M lock: cascade-deleted descendants do NOT leak
        through ``node.children`` after the fix.
        """
        async with hotfix7_session_factory() as session:
            tenant = await _make_tenant(session, "node-filter-cascade")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            module = await _make_node(
                session,
                tenant_id=tenant.id,
                parent_id=root.id,
                title="module",
            )
            topic = await _make_node(
                session,
                tenant_id=tenant.id,
                parent_id=module.id,
                title="topic",
            )
            cascade_ts = datetime.now(UTC)
            module.deleted_at = cascade_ts
            topic.deleted_at = cascade_ts
            await session.commit()
            tenant_id, root_id = tenant.id, root.id

        async with hotfix7_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree_with_active_documents(root_id)

        assert len(tree) == 1
        loaded_root = tree[0]
        assert loaded_root.id == root_id
        # Recursive CTE walk terminates at the deleted Module — Topic
        # is unreachable through the active-only walk even though its
        # parent_id still points at Module on disk.
        assert loaded_root.children == []

        await _cleanup_tenant(hotfix7_session_factory, [tenant_id])

    async def test_soft_deleted_root_returns_empty(
        self,
        hotfix7_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Caller passes a ``root_id`` that points to an already
        soft-deleted node — the base-anchor filter rejects it and the
        helper returns an empty list. Locks the symmetric base-side
        filter (would otherwise return the deleted root with empty
        children, leaking the row's existence).
        """
        async with hotfix7_session_factory() as session:
            tenant = await _make_tenant(session, "node-filter-root-deleted")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            root.deleted_at = datetime.now(UTC)
            await session.commit()
            tenant_id, root_id = tenant.id, root.id

        async with hotfix7_session_factory() as session:
            repo = CourseNodeRepository(session)
            tree = await repo.get_subtree_with_active_documents(root_id)

        assert tree == []

        await _cleanup_tenant(hotfix7_session_factory, [tenant_id])
