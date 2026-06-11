"""Integration: mixed-staleness + edge cases (Phase 3.2.2 commit 4).

Closes commit-4 acceptance hostings:

* Mixed-staleness subtree: pre-seed Raws so some nodes are axis-1 fresh
  and others are axis-1 stale. Verify selective memo-skip — hook called
  ONLY on stale nodes, fresh nodes get ``SKIPPED_MEMO``. Goes beyond
  idempotency (which is "all-fresh") by pinning the selective behavior.

* Deeply nested tree (6 levels): orchestrator handles a course with
  the maximum realistic depth without breaking either order algorithm
  or per-node atomicity.

* Multiple force runs: each run records ``force`` into run-state; the
  K1 invariant (memo-skip ignores force) holds across consecutive
  force=True runs over the same vertex.

Run with: ``uv run pytest -m requires_db --run-db``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.jobs import JobType
from course_supporter.storage.node_summary_orchestrator import (
    NodeSummaryGenerationOrchestrator,
)
from course_supporter.storage.node_summary_run_state import (
    NodeSummaryNodeStatus,
    NodeSummaryRunState,
)
from course_supporter.storage.orm import (
    CourseNode,
    Job,
    NodeSummaryFinal,
    NodeSummaryRaw,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


# ── Recording methodist ───────────────────────────────────────────


class _RecordingMethodist:
    def __init__(self) -> None:
        self.bottomup_calls: list[uuid.UUID] = []
        self.topdown_calls: list[uuid.UUID] = []

    async def generate_bottomup(self, node: CourseNode, raw: NodeSummaryRaw) -> None:
        self.bottomup_calls.append(node.id)

    async def generate_topdown(
        self,
        node: CourseNode,
        raw: NodeSummaryRaw,
        parent_raw: NodeSummaryRaw,
    ) -> None:
        self.topdown_calls.append(node.id)


async def _cleanup_tenant(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(NodeSummaryFinal).where(
                NodeSummaryFinal.course_node_id.in_(
                    select(CourseNode.id).where(CourseNode.tenant_id == tenant_id)
                )
            )
        )
        await session.execute(
            delete(NodeSummaryRaw).where(
                NodeSummaryRaw.course_node_id.in_(
                    select(CourseNode.id).where(CourseNode.tenant_id == tenant_id)
                )
            )
        )
        await session.execute(delete(Job).where(Job.tenant_id == tenant_id))
        await session.execute(
            delete(CourseNode).where(CourseNode.tenant_id == tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


# ── Mixed-staleness ───────────────────────────────────────────────


class TestMixedStalenessSelectiveMemoSkip:
    """Selective memo-skip: hook called ONLY on axis-1-stale in-scope nodes."""

    async def test_selective_memo_skip_matches_staleness_state(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            tenant = Tenant(name=f"mixed-{uuid.uuid4().hex[:8]}")
            session.add(tenant)
            await session.flush()

            root = make_root_course_node(tenant_id=tenant.id, title="root", order=0)
            session.add(root)
            await session.flush()

            a = CourseNode(tenant_id=tenant.id, parent_id=root.id, title="a", order=0)
            b = CourseNode(tenant_id=tenant.id, parent_id=root.id, title="b", order=1)
            session.add_all([a, b])
            await session.flush()

            a1 = CourseNode(tenant_id=tenant.id, parent_id=a.id, title="a1", order=0)
            a2 = CourseNode(tenant_id=tenant.id, parent_id=a.id, title="a2", order=1)
            session.add_all([a1, a2])
            await session.flush()

            # Pre-seed Raws with mixed staleness:
            #  - a:  Raw with source_content_hash == a.content_hash → fresh
            #  - a2: Raw with source_content_hash == a2.content_hash → fresh
            #  - b:  Raw with WRONG source_content_hash → stale
            #  - root, a1: NO Raw → stale
            session.add(
                NodeSummaryRaw(course_node_id=a.id, source_content_hash=a.content_hash)
            )
            session.add(
                NodeSummaryRaw(
                    course_node_id=a2.id, source_content_hash=a2.content_hash
                )
            )
            session.add(
                NodeSummaryRaw(course_node_id=b.id, source_content_hash="f" * 64)
            )

            job = Job(
                tenant_id=tenant.id,
                course_node_id=root.id,
                job_type=JobType.NODE_SUMMARY_REGENERATION,
                status="active",
            )
            session.add(job)
            await session.flush()
            await session.commit()

            tenant_id = tenant.id
            job_id = job.id
            ids = {
                "root": root.id,
                "a": a.id,
                "b": b.id,
                "a1": a1.id,
                "a2": a2.id,
            }
        try:
            methodist = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(session, methodist=methodist)
                await orch.run(job_id=job_id, vertex_node_id=ids["root"])

            # Pass 1 hook called ONLY on stale nodes: root, b, a1.
            # Memo-skipped nodes (a, a2) get NO hook call.
            assert set(methodist.bottomup_calls) == {
                ids["root"],
                ids["b"],
                ids["a1"],
            }

            async with session_factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                assert state.pass1[ids["a"]] is NodeSummaryNodeStatus.SKIPPED_MEMO
                assert state.pass1[ids["a2"]] is NodeSummaryNodeStatus.SKIPPED_MEMO
                assert state.pass1[ids["root"]] is NodeSummaryNodeStatus.DONE
                assert state.pass1[ids["b"]] is NodeSummaryNodeStatus.DONE
                assert state.pass1[ids["a1"]] is NodeSummaryNodeStatus.DONE
        finally:
            await _cleanup_tenant(session_factory, tenant_id)


# ── Deeply nested tree (6 levels) ─────────────────────────────────


class TestDeeplyNestedTree:
    """6-level deep course: ordering + atomicity hold at depth."""

    async def test_six_level_tree_processes_in_order(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            tenant = Tenant(name=f"deep-{uuid.uuid4().hex[:8]}")
            session.add(tenant)
            await session.flush()

            # root → l1 → l2 → l3 → l4 → l5 (single chain, 6 levels)
            root = make_root_course_node(tenant_id=tenant.id, title="root", order=0)
            session.add(root)
            await session.flush()
            chain: list[CourseNode] = [root]
            parent: CourseNode = root
            for i in range(1, 6):
                child = CourseNode(
                    tenant_id=tenant.id,
                    parent_id=parent.id,
                    title=f"l{i}",
                    order=0,
                )
                session.add(child)
                await session.flush()
                chain.append(child)
                parent = child

            job = Job(
                tenant_id=tenant.id,
                course_node_id=root.id,
                job_type=JobType.NODE_SUMMARY_REGENERATION,
                status="active",
            )
            session.add(job)
            await session.flush()
            await session.commit()

            tenant_id = tenant.id
            job_id = job.id
            chain_ids = [n.id for n in chain]
        try:
            methodist = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(session, methodist=methodist)
                await orch.run(job_id=job_id, vertex_node_id=chain_ids[0])

            # Pass 1 (leaf→root) — l5, l4, l3, l2, l1, root.
            assert methodist.bottomup_calls == [
                chain_ids[5],
                chain_ids[4],
                chain_ids[3],
                chain_ids[2],
                chain_ids[1],
                chain_ids[0],
            ]
            # Pass 2 (root→leaves, root NOT_APPLICABLE) — l1, l2, l3, l4, l5.
            assert methodist.topdown_calls == [
                chain_ids[1],
                chain_ids[2],
                chain_ids[3],
                chain_ids[4],
                chain_ids[5],
            ]

            async with session_factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                for nid in chain_ids:
                    assert state.pass1[nid] is NodeSummaryNodeStatus.DONE
                assert state.pass2[chain_ids[0]] is NodeSummaryNodeStatus.NOT_APPLICABLE
                for nid in chain_ids[1:]:
                    assert state.pass2[nid] is NodeSummaryNodeStatus.DONE
        finally:
            await _cleanup_tenant(session_factory, tenant_id)


# ── Multiple force runs ───────────────────────────────────────────


class TestMultipleForceRuns:
    """Successive force=True runs each record force; memo-skip ignores it."""

    async def test_two_force_runs_both_record_force_and_memo_skip(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            tenant = Tenant(name=f"force-{uuid.uuid4().hex[:8]}")
            session.add(tenant)
            await session.flush()
            root = make_root_course_node(tenant_id=tenant.id, title="root", order=0)
            session.add(root)
            await session.flush()
            a = CourseNode(tenant_id=tenant.id, parent_id=root.id, title="a", order=0)
            session.add(a)
            await session.flush()
            job = Job(
                tenant_id=tenant.id,
                course_node_id=root.id,
                job_type=JobType.NODE_SUMMARY_REGENERATION,
                status="active",
            )
            session.add(job)
            await session.flush()
            await session.commit()
            tenant_id = tenant.id
            job_id = job.id
            root_id = root.id
        try:
            # Run 1 — force=True, vertex=root, no uncovered_stale.
            methodist1 = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(session, methodist=methodist1)
                await orch.run(job_id=job_id, vertex_node_id=root_id, force=True)

            async with session_factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                assert state.force is True

            # Run 2 — force=True again. K1 invariant: memo-skip fires
            # despite force (axis-1-fresh nodes are NOT regenerated).
            methodist2 = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(session, methodist=methodist2)
                await orch.run(job_id=job_id, vertex_node_id=root_id, force=True)
            # Zero hook calls on run 2 — memo-skip ignores force.
            assert methodist2.bottomup_calls == []
            assert methodist2.topdown_calls == []

            async with session_factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                assert state.force is True
        finally:
            await _cleanup_tenant(session_factory, tenant_id)


# ── Final acceptance #1 sanity: empty-hash literals match formula ─


class TestEmptyHashLiteralsParitySanity:
    """Sanity: server_default constants match the formula-derived values.

    Phase 3.1 ``test_empty_hash_literals`` already pins this for the three
    empty-hash literals. This is a smoke-confirmation that the same
    empty-content invariant holds for orchestrator-driven flow: a node
    visited with the no-op hook ends with ``raw.content_hash`` equal to
    the ``EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH`` literal.
    """

    async def test_no_op_hook_leaves_content_hash_at_empty_default(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            tenant = Tenant(name=f"empty-{uuid.uuid4().hex[:8]}")
            session.add(tenant)
            await session.flush()
            root = make_root_course_node(tenant_id=tenant.id, title="root", order=0)
            session.add(root)
            await session.flush()
            job = Job(
                tenant_id=tenant.id,
                course_node_id=root.id,
                job_type=JobType.NODE_SUMMARY_REGENERATION,
                status="active",
            )
            session.add(job)
            await session.flush()
            await session.commit()
            tenant_id = tenant.id
            job_id = job.id
            root_id = root.id
        try:
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_RecordingMethodist()
                )
                await orch.run(job_id=job_id, vertex_node_id=root_id)

            async with session_factory() as session:
                result = await session.execute(
                    select(NodeSummaryRaw).where(
                        NodeSummaryRaw.course_node_id == root_id
                    )
                )
                raw = result.scalar_one()
                # No-op hook never touched content fields → empty default.
                from course_supporter.storage.content_hash import (
                    EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH,
                )

                assert raw.content_hash == EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH
        finally:
            await _cleanup_tenant(session_factory, tenant_id)
