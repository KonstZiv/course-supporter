"""Integration: Pass 2 leg of NodeSummaryGenerationOrchestrator (commit 3).

Pins commit-3 acceptance:

* #4 (full) — Pass 1 + Pass 2 ordering: bottom-up leaves→root precedes
  top-down root→leaves; course-root is root-of-course, not vertex.
* #5 (full) — None-contract: ``Pass2ParentRawMissingError`` raised
  internally with A1/A2/B discrimination when non-root parent Raw is
  missing; recorded into ``errors[]`` + ``pass2[node]=ERROR``; walk
  continues; ``update_source_hash`` is NEVER called with ``None``.
* #6 (Pass 2 partial) — Pass 2 per-node statuses written to
  ``Job.stage_progress``; ``Job.current_stage`` advances
  ``bottomup → topdown``.
* #7 (skeleton-form) — Pass 2 memo-skip does NOT touch
  ``NodeSummaryFinal.enclosing_context_updated_at`` (inverted pin via
  pre-seeded Final row + post-run unchanged assertion). Skeleton 3.2.2
  does not write Final at all; this test future-proofs against
  accidental writes when 3.2.4 lands the third channel.
* Bonus pin: Pass 2 idempotency — second run all SKIPPED_MEMO (root
  ``NOT_APPLICABLE``); ``generate_topdown`` called zero times on rerun.

Full crash-resume integration coverage lands in commit 4.

Run with: ``uv run pytest -m requires_db --run-db``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

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


# ── Tree + Job setup ──────────────────────────────────────────────


async def _setup_course_with_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Build root + 3 children + 2 grand-children + active Job; commit."""
    async with session_factory() as session:
        tenant = Tenant(name=f"pass2-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        root = make_root_course_node(tenant_id=tenant.id, title="root", order=0)
        session.add(root)
        await session.flush()

        a = CourseNode(tenant_id=tenant.id, parent_id=root.id, title="a", order=0)
        b = CourseNode(tenant_id=tenant.id, parent_id=root.id, title="b", order=1)
        c = CourseNode(tenant_id=tenant.id, parent_id=root.id, title="c", order=2)
        session.add_all([a, b, c])
        await session.flush()

        a1 = CourseNode(tenant_id=tenant.id, parent_id=a.id, title="a1", order=0)
        a2 = CourseNode(tenant_id=tenant.id, parent_id=a.id, title="a2", order=1)
        session.add_all([a1, a2])
        await session.flush()

        job = Job(
            tenant_id=tenant.id,
            course_node_id=root.id,
            job_type=JobType.NODE_SUMMARY_REGENERATION,
            status="active",
            input_params={"vertex_node_id": str(root.id), "force": False},
        )
        session.add(job)
        await session.flush()
        await session.commit()

        return {
            "tenant_id": tenant.id,
            "job_id": job.id,
            "root": root.id,
            "a": a.id,
            "b": b.id,
            "c": c.id,
            "a1": a1.id,
            "a2": a2.id,
        }


async def _cleanup_course(
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


# ── Recording methodist ───────────────────────────────────────────


class _RecordingMethodist:
    """Records bottom-up + top-down call order; never raises."""

    def __init__(self) -> None:
        self.bottomup_calls: list[uuid.UUID] = []
        self.topdown_calls: list[uuid.UUID] = []
        # Capture the (node_id, parent_raw_present) tuple so tests can pin
        # that parent_raw is never None (None-contract negative pin).
        self.topdown_parent_present: list[tuple[uuid.UUID, bool]] = []

    async def generate_bottomup(self, node: CourseNode, raw: NodeSummaryRaw) -> None:
        self.bottomup_calls.append(node.id)

    async def generate_topdown(
        self,
        node: CourseNode,
        raw: NodeSummaryRaw,
        parent_raw: NodeSummaryRaw,
    ) -> None:
        self.topdown_calls.append(node.id)
        self.topdown_parent_present.append((node.id, parent_raw is not None))


# ── #4 ordering: leaf→root precedes root→leaves ───────────────────


class TestPassOrdering:
    """Acceptance #4 (full)."""

    async def test_bottomup_finishes_before_topdown_starts(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            methodist = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(session, methodist=methodist)
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Pass 1 visited every node; Pass 2 visited every non-root.
            assert set(methodist.bottomup_calls) == {
                ids["root"],
                ids["a"],
                ids["b"],
                ids["c"],
                ids["a1"],
                ids["a2"],
            }
            # Pass 2 root is NOT_APPLICABLE → hook not called for root.
            assert ids["root"] not in methodist.topdown_calls
            assert set(methodist.topdown_calls) == {
                ids["a"],
                ids["b"],
                ids["c"],
                ids["a1"],
                ids["a2"],
            }

            # Pass 2 ordering: parent precedes children (root→leaves).
            pos = {nid: idx for idx, nid in enumerate(methodist.topdown_calls)}
            assert pos[ids["a"]] < pos[ids["a1"]]
            assert pos[ids["a"]] < pos[ids["a2"]]

            # Sanity: parent_raw was non-None on every topdown invocation.
            assert all(present for _, present in methodist.topdown_parent_present)
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── root NOT_APPLICABLE + current_stage transition ────────────────


class TestRootNotApplicable:
    """Pass 2 on course root → ``NOT_APPLICABLE`` (KD10 line 1001)."""

    async def test_root_marked_not_applicable_and_stage_advances(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_RecordingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                assert state.pass2[ids["root"]] is NodeSummaryNodeStatus.NOT_APPLICABLE
                # Non-root nodes all DONE.
                for nid in (
                    ids["a"],
                    ids["b"],
                    ids["c"],
                    ids["a1"],
                    ids["a2"],
                ):
                    assert state.pass2[nid] is NodeSummaryNodeStatus.DONE
                # current_stage finished on 'topdown' (last value written).
                assert job.current_stage == "topdown"
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── inner vertex with out-of-scope parent ──────────────────────────


class TestInnerVertexWithOutOfScopeParent:
    """Vertex ≠ course root, parent has Raw outside scope → vertex Pass 2 OK."""

    async def test_inner_vertex_processes_normally_with_outscope_parent(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # First run with vertex=root populates Raws across the whole course.
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_RecordingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Simulate axis-1 + axis-2 staleness on 'a' so Pass 2 has to
            # actually invoke the hook (rather than memo-skip). Flip 'a's
            # content_hash AND clear its axis-2 source_hash. The parent of
            # 'a' is 'root', which is out_of_scope when vertex='a'; 'root'
            # HAS Raw from the first run, so the orchestrator must fetch
            # that out-of-scope parent's Raw correctly.
            async with session_factory() as session:
                node_a = await session.get(CourseNode, ids["a"])
                assert node_a is not None
                node_a.content_hash = "a" * 64
                raw_a = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["a"]
                        )
                    )
                ).scalar_one()
                raw_a.enclosing_context_source_hash = None
                await session.commit()

            methodist_second = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=methodist_second
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["a"])

            # Pass 1: 'a' axis-1 stale → hook called.
            assert ids["a"] in methodist_second.bottomup_calls
            # Pass 2: 'a' axis-2 stale → hook called WITH out-of-scope
            # parent_raw (root's Raw). The negative pin on None-contract:
            # parent_raw was non-None.
            assert ids["a"] in methodist_second.topdown_calls
            a_invocation = next(
                (
                    present
                    for nid, present in methodist_second.topdown_parent_present
                    if nid == ids["a"]
                ),
                None,
            )
            assert a_invocation is True

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                # 'a' visited because parent (root) has Raw — no raise.
                assert state.pass2[ids["a"]] is NodeSummaryNodeStatus.DONE
                # No ERROR entries on the path.
                error_node_ids = {e.node_id for e in state.errors}
                assert ids["a"] not in error_node_ids
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── #5 None-contract — A1/A2/B discrimination ─────────────────────


class TestNoneContractDiscrimination:
    """Pass 2 raises ``Pass2ParentRawMissingError`` with proper discriminator."""

    async def test_force_bypasses_uncovered_then_pass2_errors_on_vertex(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Build a course where 'a' is the vertex AND 'root' is uncovered_stale
        # (root has no Raw). validate_scope flags root in uncovered_stale; user
        # uses force=True; Pass 2 on 'a' fetches parent Raw → None → raises.
        ids = await _setup_course_with_job(session_factory)
        try:
            methodist_calls: list[uuid.UUID] = []

            class _ObservedMethodist:
                async def generate_bottomup(
                    self, node: CourseNode, raw: NodeSummaryRaw
                ) -> None:
                    methodist_calls.append(node.id)

                async def generate_topdown(
                    self,
                    node: CourseNode,
                    raw: NodeSummaryRaw,
                    parent_raw: NodeSummaryRaw,
                ) -> None:
                    # If we ever get here for 'a', parent_raw is non-None
                    # by orchestrator contract.
                    pass

            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_ObservedMethodist()
                )
                # vertex = 'a'; root is uncovered (no Raw); force=True.
                await orch.run(
                    job_id=ids["job_id"],
                    vertex_node_id=ids["a"],
                    force=True,
                )

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})

                # 'a' pass2 = ERROR because root (parent) has no Raw.
                assert state.pass2[ids["a"]] is NodeSummaryNodeStatus.ERROR

                # Error entry references topdown stage and node 'a';
                # discriminator-bearing message identifies sub-case A2 (out-
                # of-scope parent without Raw — uncovered_stale bypassed by
                # force=True).
                a_errors = [
                    e
                    for e in state.errors
                    if e.node_id == ids["a"] and e.stage == "topdown"
                ]
                assert len(a_errors) == 1
                assert "A2" in a_errors[0].reason
                assert "uncovered_stale" in a_errors[0].reason

                # Children of 'a' (a1, a2) Pass 2 succeed because their parent
                # ('a') HAS Raw from Pass 1 — walk continues after the error.
                # But: a's enclosing_context is NULL, so children's computed
                # source_hash is hash(""), which equals their stored value
                # (NULL ≠ computed hash → ERROR? no — first time around,
                # children's stored hash is NULL, computed is hex → ≠ → DONE).
                for nid in (ids["a1"], ids["a2"]):
                    assert state.pass2[nid] in {
                        NodeSummaryNodeStatus.DONE,
                        NodeSummaryNodeStatus.SKIPPED_MEMO,
                    }

                # Verify Raw of 'a' did NOT get enclosing_context_source_hash
                # written (we did not call update_source_hash on ERROR path).
                result = await session.execute(
                    select(NodeSummaryRaw).where(
                        NodeSummaryRaw.course_node_id == ids["a"]
                    )
                )
                raw_a = result.scalar_one()
                assert raw_a.enclosing_context_source_hash is None
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── #7 inverted pin — Final.enclosing_context_updated_at untouched ─


class TestFinalUpdatedAtUntouched:
    """Skeleton 3.2.2 does not write Final; pre-seeded timestamp survives."""

    async def test_pass2_memo_skip_does_not_touch_final_timestamp(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # First run — populate everything axis-1 + axis-2 fresh.
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_RecordingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Seed a NodeSummaryFinal with a pre-existing timestamp on the
            # non-root inner node 'a' (any Final-bearing node would do).
            sentinel = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
            async with session_factory() as session:
                final = NodeSummaryFinal(
                    course_node_id=ids["a"],
                    enclosing_context_updated_at=sentinel,
                    approved_at=sentinel,
                )
                session.add(final)
                await session.commit()

            # Second run — every Pass 2 should memo-skip (no work),
            # in particular not touching Final.
            methodist_second = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=methodist_second
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])
            assert methodist_second.topdown_calls == []

            # Assert Final timestamp untouched.
            async with session_factory() as session:
                result = await session.execute(
                    select(NodeSummaryFinal).where(
                        NodeSummaryFinal.course_node_id == ids["a"]
                    )
                )
                final_after = result.scalar_one()
                assert final_after.enclosing_context_updated_at == sentinel
                assert final_after.approved_at == sentinel
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── Pass 2 idempotency on rerun ────────────────────────────────────


class TestPass2Idempotency:
    """Second run with stable second-axis state → all SKIPPED_MEMO / NOT_APPLICABLE."""

    async def test_second_run_skips_topdown_everywhere(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # First run.
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_RecordingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Second run.
            methodist_second = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=methodist_second
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])
            assert methodist_second.topdown_calls == []
            assert methodist_second.bottomup_calls == []

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})

                # Root NOT_APPLICABLE.
                assert state.pass2[ids["root"]] is NodeSummaryNodeStatus.NOT_APPLICABLE
                # All other nodes SKIPPED_MEMO on second run.
                for nid in (
                    ids["a"],
                    ids["b"],
                    ids["c"],
                    ids["a1"],
                    ids["a2"],
                ):
                    assert state.pass2[nid] is NodeSummaryNodeStatus.SKIPPED_MEMO
                    assert state.pass1[nid] is NodeSummaryNodeStatus.SKIPPED_MEMO
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── enclosing_context_source_hash materialised ─────────────────────


class TestEnclosingContextSourceHashMaterialised:
    """After a clean run, every non-root Raw has axis-2 source-hash written."""

    async def test_axis2_hash_written_for_non_root_nodes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_RecordingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            async with session_factory() as session:
                result = await session.execute(
                    select(NodeSummaryRaw).where(
                        NodeSummaryRaw.course_node_id.in_(
                            [
                                ids["root"],
                                ids["a"],
                                ids["b"],
                                ids["c"],
                                ids["a1"],
                                ids["a2"],
                            ]
                        )
                    )
                )
                by_node = {r.course_node_id: r for r in result.scalars().all()}
                # Root: not touched by Pass 2 → enclosing_context_source_hash
                # remains NULL.
                assert by_node[ids["root"]].enclosing_context_source_hash is None
                # Non-root: every node has axis-2 hash materialised.
                for nid in (ids["a"], ids["b"], ids["c"], ids["a1"], ids["a2"]):
                    assert by_node[nid].enclosing_context_source_hash is not None
                    assert len(by_node[nid].enclosing_context_source_hash) == 64
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])
