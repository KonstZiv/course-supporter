"""Integration: crash-resume acceptance (Phase 3.2.2 commit 4).

Closes acceptance #6 (FULL) of the task spec — "падіння всередині
проходу лишає закомічені вузли цілими, перезапуск продовжує":

* Pass 1 crash on one node → walk continues per K2 (failed node ERROR,
  others DONE); re-run with clean methodist memo-skips the DONE nodes
  and retries ONLY the failed node.
* Pass 2 crash on one node → analogous semantics with topdown.
* Crash on Pass 1 of node X does NOT block Pass 2 of node X in the
  SAME run — ensure-Raw created the row before the hook, so visit_pass2
  finds the Raw row and proceeds (X has source_content_hash NULL, but
  visit_pass2 only needs the row to exist + a parent_raw).
* errors[] accumulates across runs (Q-4 ratify cross-run contract):
  run 1 records 1 error → run 2 succeeds → errors[] still carries
  the run-1 entry.

Crash injection via the ``MethodistGenerator`` DI seam (NOT
monkeypatch). The fault generator raises RuntimeError on the N-th
hook call only; subsequent calls succeed — matches the cleanest
resume-property pin (one failed node, exact retry assertion).

Run with: ``uv run pytest -m requires_db --run-db``.
"""

from __future__ import annotations

import uuid
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
        tenant = Tenant(name=f"resume-{uuid.uuid4().hex[:8]}")
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


# ── Fault generators (DI seam — NOT monkeypatch) ──────────────────


class _RaiseOnNthBottomupMethodist:
    """Raises RuntimeError on the N-th ``generate_bottomup`` call only.

    All other calls (before and after) succeed (no-op). Pins the exact
    resume semantics: one failed node, every other DONE on first run;
    re-run retries ONLY the failed node.
    """

    def __init__(self, *, raise_on_call: int) -> None:
        self._raise_on = raise_on_call
        self.bottomup_calls: list[uuid.UUID] = []
        self.topdown_calls: list[uuid.UUID] = []

    async def generate_bottomup(self, node: CourseNode, raw: NodeSummaryRaw) -> None:
        self.bottomup_calls.append(node.id)
        if len(self.bottomup_calls) == self._raise_on:
            raise RuntimeError("synthetic crash on N-th bottomup")

    async def generate_topdown(
        self,
        node: CourseNode,
        raw: NodeSummaryRaw,
        parent_raw: NodeSummaryRaw,
    ) -> None:
        self.topdown_calls.append(node.id)


class _RaiseOnNthTopdownMethodist:
    """Raises RuntimeError on the N-th ``generate_topdown`` call only."""

    def __init__(self, *, raise_on_call: int) -> None:
        self._raise_on = raise_on_call
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
        if len(self.topdown_calls) == self._raise_on:
            raise RuntimeError("synthetic crash on N-th topdown")


class _RecordingMethodist:
    """Clean methodist — no faults. Records call order."""

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


# ── #6 FULL — Pass 1 crash-resume ─────────────────────────────────


class TestPass1ResumeAfterError:
    """Crash on one Pass 1 hook → re-run retries ONLY that node."""

    async def test_run1_records_one_error_run2_retries_only_failed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # Run 1 — crash on the 3rd Pass-1 hook call.
            crash_methodist = _RaiseOnNthBottomupMethodist(raise_on_call=3)
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=crash_methodist
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # The 3rd visited node is the failed one. Capture it via the
            # recorded call order.
            assert len(crash_methodist.bottomup_calls) == 6  # K2 continues
            failed_node_id = crash_methodist.bottomup_calls[2]

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                # Only the failed node is ERROR; others are DONE.
                for nid in ids.values():
                    if not isinstance(nid, uuid.UUID):
                        continue
                    if nid == ids["job_id"] or nid == ids["tenant_id"]:
                        continue
                    if nid in state.pass1:
                        expected = (
                            NodeSummaryNodeStatus.ERROR
                            if nid == failed_node_id
                            else NodeSummaryNodeStatus.DONE
                        )
                        assert state.pass1[nid] is expected
                # One errors[] entry references the failed node + 'bottomup'.
                assert len(state.errors) == 1
                assert state.errors[0].node_id == failed_node_id
                assert state.errors[0].stage == "bottomup"

            # Run 2 — clean methodist; expect ONLY the failed node retried.
            clean_methodist = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=clean_methodist
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            assert clean_methodist.bottomup_calls == [failed_node_id]

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                # Recovered: failed node now DONE.
                assert state.pass1[failed_node_id] is NodeSummaryNodeStatus.DONE
                # All other nodes SKIPPED_MEMO (no hook called).
                for nid in (
                    ids["root"],
                    ids["a"],
                    ids["b"],
                    ids["c"],
                    ids["a1"],
                    ids["a2"],
                ):
                    if nid == failed_node_id:
                        continue
                    assert state.pass1[nid] is NodeSummaryNodeStatus.SKIPPED_MEMO
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── #6 FULL — Pass 2 crash-resume ─────────────────────────────────


class TestPass2ResumeAfterError:
    """Crash on one Pass 2 hook → re-run retries ONLY that node."""

    async def test_run1_topdown_error_run2_retries_only_failed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # Pass 2 raises on its 3rd call. With 5 non-root in_scope nodes,
            # the 3rd top-down call will land on whatever the orchestrator's
            # root→leaves order produces — we capture it dynamically.
            crash_methodist = _RaiseOnNthTopdownMethodist(raise_on_call=3)
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=crash_methodist
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # 5 non-root nodes were visited in Pass 2 (root is NOT_APPLICABLE).
            assert len(crash_methodist.topdown_calls) == 5
            failed_node_id = crash_methodist.topdown_calls[2]

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                assert state.pass2[failed_node_id] is NodeSummaryNodeStatus.ERROR
                # All other non-root nodes DONE; root NOT_APPLICABLE.
                assert state.pass2[ids["root"]] is NodeSummaryNodeStatus.NOT_APPLICABLE
                # One errors[] entry — topdown stage.
                topdown_errors = [e for e in state.errors if e.stage == "topdown"]
                assert len(topdown_errors) == 1
                assert topdown_errors[0].node_id == failed_node_id

            # Run 2 — clean methodist; only the failed node retried in Pass 2.
            clean_methodist = _RecordingMethodist()
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=clean_methodist
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])
            # Pass 1 fully memo-skips (all source_content_hash written in run 1).
            assert clean_methodist.bottomup_calls == []
            # Pass 2 retries only the failed node.
            assert clean_methodist.topdown_calls == [failed_node_id]
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── Pass 1 ERROR does not block Pass 2 on same node in same run ───


class TestCrashInPass1NodePassesPass2InSameRun:
    """Pass 1 ERROR on node X does NOT prevent Pass 2 on X in the same run.

    ensure-Raw creates the row BEFORE the hook, so even when the hook
    raises, the row exists with ``source_content_hash=NULL``. Pass 2
    then finds the row, fetches the parent's Raw, and proceeds.
    """

    async def test_pass2_succeeds_on_pass1_errored_node(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            crash_methodist = _RaiseOnNthBottomupMethodist(raise_on_call=1)
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=crash_methodist
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # 1st bottomup call is the first leaf (a1 in leaf→root order
            # within the {root,a,a1,a2,b,c} subtree). Capture the failed id.
            failed_id = crash_methodist.bottomup_calls[0]
            # Pass 2 of failed_id still ran. Confirm via state.
            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                # Pass 1 failed_id = ERROR.
                assert state.pass1[failed_id] is NodeSummaryNodeStatus.ERROR
                # Pass 2 failed_id = DONE (Pass 1 ERROR didn't block Pass 2).
                # Unless failed_id is the course root, in which case
                # NOT_APPLICABLE. With raise_on_call=1 and leaf→root order,
                # failed_id is always a leaf — never root.
                assert state.pass2[failed_id] is NodeSummaryNodeStatus.DONE
                # ensure-Raw row exists with source_content_hash=NULL
                # (hook never reached materialise step) but
                # enclosing_context_source_hash IS populated (Pass 2 DONE).
                result = await session.execute(
                    select(NodeSummaryRaw).where(
                        NodeSummaryRaw.course_node_id == failed_id
                    )
                )
                raw = result.scalar_one()
                assert raw.source_content_hash is None
                assert raw.enclosing_context_source_hash is not None
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── Q-4 cross-run errors[] preservation ───────────────────────────


class TestErrorsPreservedAcrossRuns:
    """``errors[]`` accumulates across runs (Q-4 ratify contract)."""

    async def test_errors_from_first_run_survive_second_run(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # Run 1 — crash on 2nd bottomup; one error recorded.
            crash_methodist = _RaiseOnNthBottomupMethodist(raise_on_call=2)
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=crash_methodist
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                run1_error_count = len(state.errors)
                assert run1_error_count == 1
                run1_error_reason = state.errors[0].reason

            # Run 2 — clean methodist; no NEW errors, but run-1 entry preserved.
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_RecordingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                # Run-1 error preserved verbatim.
                assert len(state.errors) == 1
                assert state.errors[0].reason == run1_error_reason

            # Run 3 — crash again (different node); errors[] grows to 2.
            crash_methodist_3 = _RaiseOnNthBottomupMethodist(raise_on_call=1)
            # But run 3 starts with all axis-1-fresh from run 2 → memo-skip
            # all of Pass 1 → no hook calls → no new errors. Use a flipped
            # CourseNode.content_hash to force regeneration.
            async with session_factory() as session:
                # Flip every node's content_hash → axis-1 stale everywhere.
                result = await session.execute(
                    select(CourseNode).where(CourseNode.tenant_id == ids["tenant_id"])
                )
                for node in result.scalars().all():
                    node.content_hash = uuid.uuid4().hex + "0" * 32
                await session.commit()

            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=crash_methodist_3
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                # Two errors now — original preserved + new one appended.
                assert len(state.errors) == 2
                assert state.errors[0].reason == run1_error_reason
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── Q-4 condition 1 — tolerant read-back on malformed errors entry ─


class TestReadBackToleratesMalformedEntries:
    """Bad error entry → log + skip; good entries preserved."""

    async def test_malformed_error_entry_skipped(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # Hand-craft a stage_progress payload with one good + one bad entry.
            good_entry = {
                "node_id": str(uuid.uuid4()),
                "stage": "bottomup",
                "reason": "good entry",
                "at": "2026-06-11T00:00:00+00:00",
            }
            bad_entry = {"this_is_not": "a valid error entry"}
            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                job.stage_progress = {"errors": [good_entry, bad_entry]}
                await session.commit()

            # Run — read-back must skip the bad entry, preserve the good one.
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_RecordingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            async with session_factory() as session:
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                state = NodeSummaryRunState.from_jsonb(job.stage_progress or {})
                # Good entry preserved; bad entry dropped.
                assert len(state.errors) == 1
                assert state.errors[0].reason == "good entry"
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])
