"""Integration: NodeSummary{Raw,Final,FinalPreviousSnapshot} against real PostgreSQL.

Covers Phase 3.1 acceptance #3 (CourseNode INSERT carries the
defined empty-hash) and acceptance #5 (cascade soft-delete of a
CourseNode sets ``deleted_at`` on its NodeSummaryRaw / Final /
PreviousSnapshot through the wired ``__cascades_soft_delete_to__``
declaration).

Run with: ``uv run pytest -m requires_db --run-db``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.cascade import (
    CascadeDeleteService,
    build_cascade_map,
)
from course_supporter.storage.content_hash import (
    EMPTY_NODE_CONTENT_HASH,
    EMPTY_NODE_SUMMARY_FINAL_CONTENT_HASH,
    EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH,
)
from course_supporter.storage.orm import (
    CourseNode,
    NodeSummaryFinal,
    NodeSummaryFinalPreviousSnapshot,
    NodeSummaryRaw,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node
from tests._helpers.kd3_marker import assert_marker_recent

pytestmark = pytest.mark.requires_db


# ── Fixture helpers ──────────────────────────────────────────────


async def _make_tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name=f"ns-test-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _make_root_node(session: AsyncSession, tenant_id: uuid.UUID) -> CourseNode:
    node = make_root_course_node(tenant_id=tenant_id, title="root")
    session.add(node)
    await session.flush()
    return node


async def _make_raw(session: AsyncSession, course_node_id: uuid.UUID) -> NodeSummaryRaw:
    raw = NodeSummaryRaw(course_node_id=course_node_id)
    session.add(raw)
    await session.flush()
    return raw


async def _make_final(
    session: AsyncSession, course_node_id: uuid.UUID
) -> NodeSummaryFinal:
    final = NodeSummaryFinal(course_node_id=course_node_id)
    session.add(final)
    await session.flush()
    return final


async def _make_previous_snapshot(
    session: AsyncSession, node_summary_final_id: uuid.UUID
) -> NodeSummaryFinalPreviousSnapshot:
    snap = NodeSummaryFinalPreviousSnapshot(
        node_summary_final_id=node_summary_final_id,
    )
    session.add(snap)
    await session.flush()
    return snap


# ── Acceptance #3 — server_default empty-hash at INSERT ──────────


class TestServerDefaultEmptyHash:
    """KD9 NULL-at-INSERT regression CLOSED (Phase 3.1 Q-G).

    All three hash-bearing methodist tables — CourseNode,
    NodeSummaryRaw, NodeSummaryFinal — carry a DEFINED empty-hash
    server_default. INSERTing an empty row must NOT yield
    ``content_hash IS NULL``.
    """

    async def test_course_node_insert_carries_empty_hash(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session)
        node = await _make_root_node(db_session, tenant.id)
        # ``eager_defaults`` flushes server-side defaults onto the ORM
        # instance via RETURNING — readback should be the empty hash,
        # not NULL.
        await db_session.refresh(node)
        assert node.content_hash == EMPTY_NODE_CONTENT_HASH

    async def test_node_summary_raw_insert_carries_empty_hash(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session)
        node = await _make_root_node(db_session, tenant.id)
        raw = await _make_raw(db_session, node.id)
        await db_session.refresh(raw)
        assert raw.content_hash == EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH

    async def test_node_summary_final_insert_carries_empty_hash(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session)
        node = await _make_root_node(db_session, tenant.id)
        final = await _make_final(db_session, node.id)
        await db_session.refresh(final)
        assert final.content_hash == EMPTY_NODE_SUMMARY_FINAL_CONTENT_HASH


# ── Acceptance #5 — cascade soft-delete coverage ─────────────────


class TestCascadeSoftDelete:
    """CourseNode soft-delete cascades to NodeSummaryRaw + NodeSummaryFinal,
    and NodeSummaryFinal soft-delete cascades to PreviousSnapshot.

    Q-C Option A two-level shape: PreviousSnapshot is reached via the
    Final leg, not directly from CourseNode — mirrors the
    AuthoredDocument → DocumentSummary → DocumentSegment cascade
    pattern.
    """

    async def test_course_node_cascade_reaches_raw_and_final(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session)
        node = await _make_root_node(db_session, tenant.id)
        raw = await _make_raw(db_session, node.id)
        final = await _make_final(db_session, node.id)

        cascade_map = build_cascade_map(CourseNode)
        svc = CascadeDeleteService(db_session)
        await svc.soft_delete_with_cascade(node, cascade_map)
        await db_session.flush()

        await db_session.refresh(raw)
        await db_session.refresh(final)
        assert raw.deleted_at is not None, (
            "NodeSummaryRaw must be soft-deleted via CourseNode cascade"
        )
        assert final.deleted_at is not None, (
            "NodeSummaryFinal must be soft-deleted via CourseNode cascade"
        )

    async def test_course_node_cascade_reaches_previous_snapshot(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session)
        node = await _make_root_node(db_session, tenant.id)
        final = await _make_final(db_session, node.id)
        snap = await _make_previous_snapshot(db_session, final.id)

        cascade_map = build_cascade_map(CourseNode)
        svc = CascadeDeleteService(db_session)
        await svc.soft_delete_with_cascade(node, cascade_map)
        await db_session.flush()

        await db_session.refresh(snap)
        assert snap.deleted_at is not None, (
            "PreviousSnapshot must be soft-deleted via "
            "CourseNode → NodeSummaryFinal → PreviousSnapshot cascade"
        )

    async def test_course_node_cascade_scrubs_node_summary_content(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session)
        node = await _make_root_node(db_session, tenant.id)
        raw = NodeSummaryRaw(
            course_node_id=node.id,
            title="Original title",
            description="Original description",
            main_concepts=["alpha", "beta"],
            methodist_observations=["one"],
        )
        db_session.add(raw)
        await db_session.flush()

        cascade_map = build_cascade_map(CourseNode)
        svc = CascadeDeleteService(db_session)
        await svc.soft_delete_with_cascade(node, cascade_map)
        await db_session.flush()

        await db_session.refresh(raw)
        # KD3 markers on string content (Q-D ratify); list fields reset
        # to ``[]`` rather than carrying synthetic marker elements.
        # Marker format validated via the canonical helper
        # ``tests/_helpers/kd3_marker.assert_marker_recent`` (regex +
        # timestamp tolerance) — sibling tests in ``test_cascade_*``
        # use the same helper, so a contract change picks up here
        # atomically without per-test substring drift.
        assert raw.title is not None
        assert_marker_recent(raw.title)
        assert raw.description is not None
        assert_marker_recent(raw.description)
        assert raw.main_concepts == []
        assert raw.methodist_observations == []
