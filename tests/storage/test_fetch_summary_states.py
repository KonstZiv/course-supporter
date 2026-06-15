"""DB tests for ``CourseNodeRepository.fetch_summary_states`` (Task 3.2.5b).

Exercises the single batch ``SELECT`` against a real Postgres session:
the three ``summary_status`` values, axis-1 ``materials_changed``, and —
critically — the ``deleted_at IS NULL`` filter on BOTH join sides:

* a soft-deleted ``NodeSummaryFinal`` (even with ``approved_at`` set)
  must NOT read as ``approved`` — it counts as absent;
* a soft-deleted ``NodeSummaryRaw`` must NOT read as a summary — the
  node reads as ``none``.

An in-memory surrogate cannot exercise the active-filtered outer joins
that PostgreSQL resolves, so these run under ``requires_db``.
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

from course_supporter.config import get_settings
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.orm import (
    CourseNode,
    NodeSummaryFinal,
    NodeSummaryRaw,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = [pytest.mark.asyncio, pytest.mark.requires_db]


@pytest.fixture(scope="module")
async def engine() -> AsyncGenerator[AsyncEngine]:
    eng = create_async_engine(get_settings().database_url, pool_size=2, max_overflow=0)
    yield eng
    await eng.dispose()


@pytest.fixture()
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _make_child(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    parent_id: uuid.UUID,
    content_hash: str,
) -> CourseNode:
    node = CourseNode(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        parent_id=parent_id,
        title="case",
        order=0,
        content_hash=content_hash,
    )
    session.add(node)
    await session.flush()
    return node


async def _make_raw(
    session: AsyncSession,
    *,
    course_node_id: uuid.UUID,
    source_content_hash: str | None,
    deleted: bool = False,
) -> None:
    raw = NodeSummaryRaw(
        id=uuid.uuid4(),
        course_node_id=course_node_id,
        source_content_hash=source_content_hash,
    )
    if deleted:
        raw.deleted_at = datetime.now(UTC)
    session.add(raw)
    await session.flush()


async def _make_final(
    session: AsyncSession,
    *,
    course_node_id: uuid.UUID,
    approved: bool,
    deleted: bool = False,
) -> None:
    final = NodeSummaryFinal(
        id=uuid.uuid4(),
        course_node_id=course_node_id,
        approved_at=datetime.now(UTC) if approved else None,
    )
    if deleted:
        final.deleted_at = datetime.now(UTC)
    session.add(final)
    await session.flush()


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    from sqlalchemy import select as sa_select

    async with session_factory() as session:
        node_ids = sa_select(CourseNode.id).where(CourseNode.tenant_id == tenant_id)
        await session.execute(
            delete(NodeSummaryRaw).where(NodeSummaryRaw.course_node_id.in_(node_ids))
        )
        await session.execute(
            delete(NodeSummaryFinal).where(
                NodeSummaryFinal.course_node_id.in_(node_ids)
            )
        )
        await session.execute(
            delete(CourseNode).where(CourseNode.tenant_id == tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


async def test_fetch_summary_states_matrix(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All states + the dual-side ``deleted_at`` filter in one tree."""
    async with session_factory() as session:
        tenant = Tenant(
            id=uuid.uuid4(), name=f"sumstate-{uuid.uuid4()}", is_active=True
        )
        session.add(tenant)
        await session.flush()

        root = make_root_course_node(
            id=uuid.uuid4(), tenant_id=tenant.id, title="root", order=0
        )
        session.add(root)
        await session.flush()

        # none — no Raw at all.
        n_none = await _make_child(
            session, tenant_id=tenant.id, parent_id=root.id, content_hash="h-none"
        )
        # draft, fresh — Raw matches node hash, Final unapproved.
        n_draft = await _make_child(
            session, tenant_id=tenant.id, parent_id=root.id, content_hash="h-draft"
        )
        await _make_raw(
            session, course_node_id=n_draft.id, source_content_hash="h-draft"
        )
        await _make_final(session, course_node_id=n_draft.id, approved=False)
        # approved + materials_changed — drifted hash must NOT be masked by approval.
        n_appr = await _make_child(
            session, tenant_id=tenant.id, parent_id=root.id, content_hash="h-current"
        )
        await _make_raw(session, course_node_id=n_appr.id, source_content_hash="h-old")
        await _make_final(session, course_node_id=n_appr.id, approved=True)
        # draft — Raw active, Final SOFT-DELETED with approved_at: must read draft.
        n_delfinal = await _make_child(
            session, tenant_id=tenant.id, parent_id=root.id, content_hash="h-df"
        )
        await _make_raw(
            session, course_node_id=n_delfinal.id, source_content_hash="h-df"
        )
        await _make_final(
            session, course_node_id=n_delfinal.id, approved=True, deleted=True
        )
        # none — only a SOFT-DELETED Raw: counts as absent.
        n_delraw = await _make_child(
            session, tenant_id=tenant.id, parent_id=root.id, content_hash="h-dr"
        )
        await _make_raw(
            session,
            course_node_id=n_delraw.id,
            source_content_hash="h-dr",
            deleted=True,
        )
        await session.commit()

        repo = CourseNodeRepository(session)
        states = await repo.fetch_summary_states(
            [n_none.id, n_draft.id, n_appr.id, n_delfinal.id, n_delraw.id]
        )

    assert states[n_none.id] == ("none", False)
    assert states[n_draft.id] == ("draft", False)
    assert states[n_appr.id] == ("approved", True)
    assert states[n_delfinal.id] == ("draft", False)
    assert states[n_delraw.id] == ("none", False)

    await _cleanup(session_factory, tenant.id)


async def test_fetch_summary_states_empty_short_circuits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty node-id list returns an empty map without emitting SQL."""
    async with session_factory() as session:
        repo = CourseNodeRepository(session)
        assert await repo.fetch_summary_states([]) == {}
