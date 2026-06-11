"""Integration: Pass 1 leg of NodeSummaryGenerationOrchestrator (Phase 3.2.2 commit 2).

Pins commit-2 acceptance:

* #3 (idempotency, partial) — second run of the same scope produces no
  new Raw rows and skips every node by memoization
  (``pass1[node]=skipped_memo``); ``source_content_hash`` is stable.
* #4 (leaf→root order, partial) — for any in-scope subtree, the
  ``MethodistGenerator.generate_bottomup`` calls land children before
  parents.
* memo-skip is **unconditional** with respect to ``force`` (K1 ratify) —
  axis-1-fresh nodes are skipped even when ``force=True``; ``force``
  only widens scope at validate-time.
* error in the hook does **not** abort the run (K2 ratify) — errors
  accumulate in ``run_state.errors[]`` while the walk continues; other
  nodes still finish DONE.
* per-node atomicity (K3 ratify) — every successful visit commits;
  resume after crash sees the committed state.

Pass 2 acceptance + None-contract land in commit 3; full crash-resume
end-to-end lands in commit 4.

Run with: ``uv run pytest -m requires_db --run-db``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.jobs import JobType
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.node_summary_orchestrator import (
    NodeSummaryGenerationOrchestrator,
)
from course_supporter.storage.node_summary_run_state import (
    NodeSummaryNodeStatus,
    NodeSummaryRunState,
)
from course_supporter.storage.orm import CourseNode, Job, NodeSummaryRaw, Tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


# ── Tree + Job setup helpers (commit per call — visits commit too) ─


async def _setup_course_with_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Build root + 3 children + 2 grand-children + a fresh Job; commit.

    Returns a dict with ``tenant_id``, ``job_id``, and per-name
    ``CourseNode`` ids so the test can drive the orchestrator with a
    fresh session and verify state from a third.
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"pass1-{uuid.uuid4().hex[:8]}")
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
    """Hard-delete the test course rows (commit-aware fixtures own cleanup)."""
    from sqlalchemy import delete

    async with session_factory() as session:
        # Delete Raw + Job first (FK on course_nodes); then nodes; then tenant.
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


# ── Test methodist that records call order ─────────────────────────


class _RecordingMethodist:
    """``MethodistGenerator`` stub that records the order of bottom-up calls."""

    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def generate_bottomup(self, node: CourseNode, raw: NodeSummaryRaw) -> None:
        self.calls.append(node.id)

    async def generate_topdown(
        self,
        node: CourseNode,
        raw: NodeSummaryRaw,
        parent_raw: NodeSummaryRaw,
    ) -> None:
        return


class _RaisingOnNodeMethodist:
    """Raises on the named node; otherwise records calls (for K2 error tests)."""

    def __init__(self, raise_on: uuid.UUID) -> None:
        self._raise_on = raise_on
        self.calls: list[uuid.UUID] = []

    async def generate_bottomup(self, node: CourseNode, raw: NodeSummaryRaw) -> None:
        self.calls.append(node.id)
        if node.id == self._raise_on:
            raise RuntimeError("synthetic hook failure")

    async def generate_topdown(
        self,
        node: CourseNode,
        raw: NodeSummaryRaw,
        parent_raw: NodeSummaryRaw,
    ) -> None:
        return


# ── #3 idempotency ────────────────────────────────────────────────


class TestIdempotency:
    """Second run with stable course state hits memo-skip on every node."""

    async def test_second_run_skips_all_by_memo(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # First run — every visit is a fresh INSERT + hook + materialise.
            methodist_first = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=methodist_first
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Every in-scope node should have been visited once.
            assert set(methodist_first.calls) == {
                ids["root"],
                ids["a"],
                ids["b"],
                ids["c"],
                ids["a1"],
                ids["a2"],
            }

            # Second run — same vertex, no state changes, every node fresh.
            methodist_second = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=methodist_second
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            assert methodist_second.calls == []

            # Verify run-state on Job — pass1 all skipped_memo.
            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                assert job.stage_progress is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress)
                for nid in (
                    ids["root"],
                    ids["a"],
                    ids["b"],
                    ids["c"],
                    ids["a1"],
                    ids["a2"],
                ):
                    assert state.pass1[nid] is NodeSummaryNodeStatus.SKIPPED_MEMO

            # One Raw per CourseNode (UNIQUE + ON CONFLICT DO NOTHING).
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
                raws = result.scalars().all()
                assert len(raws) == 6  # 6 nodes → 6 Raws (no duplicates)

                # source_content_hash stable: equals CourseNode.content_hash.
                course_nodes = await session.execute(
                    select(CourseNode).where(CourseNode.tenant_id == ids["tenant_id"])
                )
                by_id = {n.id: n for n in course_nodes.scalars().all()}
                for raw in raws:
                    expected = by_id[raw.course_node_id].content_hash
                    assert raw.source_content_hash == expected
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── #4 leaf→root order ────────────────────────────────────────────


class TestLeafToRootOrder:
    """For every parent in scope, all in-scope children precede the parent."""

    async def test_children_emitted_before_parents(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            methodist = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(session, methodist=methodist)
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            positions = {nid: idx for idx, nid in enumerate(methodist.calls)}

            # a1, a2 → a → root  AND  b → root  AND  c → root.
            assert positions[ids["a1"]] < positions[ids["a"]]
            assert positions[ids["a2"]] < positions[ids["a"]]
            assert positions[ids["a"]] < positions[ids["root"]]
            assert positions[ids["b"]] < positions[ids["root"]]
            assert positions[ids["c"]] < positions[ids["root"]]
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── K1 memo-skip unconditional w.r.t. force ────────────────────────


class TestMemoSkipIgnoresForce:
    """``force=True`` does NOT regenerate axis-1-fresh nodes (K1 ratify)."""

    async def test_force_does_not_invoke_hook_on_fresh_nodes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # First run — populate source_content_hash everywhere.
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_RecordingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Second run with force=True — memo-skip still fires.
            methodist_forced = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=methodist_forced
                )
                await orch.run(
                    job_id=ids["job_id"], vertex_node_id=ids["root"], force=True
                )
            assert methodist_forced.calls == []

            # Verify force value is recorded in run-state.
            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                assert job.stage_progress is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress)
                assert state.force is True
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── K2 error-on-node does not abort the walk ───────────────────────


class TestErrorDoesNotAbortRun:
    """A raise inside one node's hook records error + status; walk continues."""

    async def test_walk_continues_after_per_node_error(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            methodist = _RaisingOnNodeMethodist(raise_on=ids["a"])
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(session, methodist=methodist)
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Hook called on every node (a1, a2, a, b, c, root may vary in
            # order between b/c but a's hook was invoked even though it raised).
            assert ids["a1"] in methodist.calls
            assert ids["a2"] in methodist.calls
            assert ids["a"] in methodist.calls
            assert ids["b"] in methodist.calls
            assert ids["c"] in methodist.calls
            assert ids["root"] in methodist.calls

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})

                # Node "a" got error status + an entry in errors[].
                assert state.pass1[ids["a"]] is NodeSummaryNodeStatus.ERROR
                assert any(
                    e.node_id == ids["a"] and e.stage == "bottomup"
                    for e in state.errors
                )

                # Other nodes completed normally.
                for nid in (
                    ids["root"],
                    ids["b"],
                    ids["c"],
                    ids["a1"],
                    ids["a2"],
                ):
                    assert state.pass1[nid] is NodeSummaryNodeStatus.DONE

                # source_content_hash written on success-path nodes;
                # NOT written on the erroring node (the materialise step
                # follows the hook).
                result = await session.execute(
                    select(NodeSummaryRaw).where(
                        NodeSummaryRaw.course_node_id.in_(
                            [ids["a"], ids["b"], ids["c"]]
                        )
                    )
                )
                by_node = {r.course_node_id: r for r in result.scalars().all()}
                assert by_node[ids["a"]].source_content_hash is None
                assert by_node[ids["b"]].source_content_hash is not None
                assert by_node[ids["c"]].source_content_hash is not None
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── K3 per-node COMMIT atomicity ────────────────────────────────


class TestPerNodeCommit:
    """Every successful visit commits; partial progress is durable."""

    async def test_first_visit_commits_before_second_starts(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # Methodist that reads committed state mid-walk via a fresh session.
            observed: list[int] = []

            class _Snooper:
                async def generate_bottomup(
                    self, node: CourseNode, raw: NodeSummaryRaw
                ) -> None:
                    # Side-channel read with a fresh session — must see the
                    # rows committed by visits that ran before this one.
                    async with session_factory() as snoop:
                        result = await snoop.execute(
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
                        observed.append(len(result.scalars().all()))

                async def generate_topdown(
                    self,
                    node: CourseNode,
                    raw: NodeSummaryRaw,
                    parent_raw: NodeSummaryRaw,
                ) -> None:
                    return

            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(session, methodist=_Snooper())
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Per-node atomicity: the orchestrator's TX commits ensure-Raw
            # + source_hash + status as one unit AFTER the hook returns,
            # so during visit N the snooper (separate session) sees exactly
            # N-1 Raws (rows from visits 1..N-1 are committed; visit N's
            # row is in the orchestrator's uncommitted TX).
            # Observation list must be [0, 1, 2, 3, 4, 5] — monotonic by
            # exactly +1 per visit. This proves both that visits commit
            # serially AND that nothing leaks before the per-node commit.
            assert observed == [0, 1, 2, 3, 4, 5]
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── Job.current_stage advances through Pass 1 ───────────────────


class TestJobStageMarker:
    """``Job.current_stage`` is set to ``'bottomup'`` once Pass 1 starts.

    Post commit-3 the final value advances to ``'topdown'`` (the last
    pass to run); the Pass 1 marker is verified mid-flight via a hook
    that snapshots ``Job.current_stage`` from a fresh session.
    """

    async def test_current_stage_is_bottomup_during_pass1(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            observed_stage: list[str | None] = []

            class _SnoopStage:
                async def generate_bottomup(
                    self, node: CourseNode, raw: NodeSummaryRaw
                ) -> None:
                    async with session_factory() as snoop:
                        job = await snoop.get(Job, ids["job_id"])
                        if job is not None:
                            observed_stage.append(job.current_stage)

                async def generate_topdown(
                    self,
                    node: CourseNode,
                    raw: NodeSummaryRaw,
                    parent_raw: NodeSummaryRaw,
                ) -> None:
                    return

            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_SnoopStage()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Every Pass 1 hook invocation saw current_stage='bottomup'.
            assert observed_stage  # at least one bottom-up hook fired
            assert all(stage == "bottomup" for stage in observed_stage)
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── update_stage_progress repo behaviour ───────────────────────


class TestUpdateStageProgressRepository:
    """``JobRepository.update_stage_progress`` writes the full JSON payload."""

    async def test_full_replace_overwrites_previous_payload(
        self, db_session: AsyncSession
    ) -> None:
        tenant = Tenant(name=f"sp-{uuid.uuid4().hex[:8]}")
        db_session.add(tenant)
        await db_session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="root", order=0)
        db_session.add(node)
        await db_session.flush()
        job = Job(
            tenant_id=tenant.id,
            course_node_id=node.id,
            job_type=JobType.NODE_SUMMARY_REGENERATION,
            status="active",
            stage_progress={"old": "payload"},
        )
        db_session.add(job)
        await db_session.flush()

        repo = JobRepository(db_session)
        await repo.update_stage_progress(job.id, {"fresh": ["payload", 2]})
        await db_session.refresh(job)
        assert job.stage_progress == {"fresh": ["payload", 2]}

    async def test_silent_skip_on_missing_job(self, db_session: AsyncSession) -> None:
        repo = JobRepository(db_session)
        # No raise — mirrors update_stage convention.
        await repo.update_stage_progress(uuid.uuid4(), {"ignored": True})
