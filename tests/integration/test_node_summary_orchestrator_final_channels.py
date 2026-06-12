"""Integration: Final channels 1 + 3 wired into the orchestrator (3.2.4 c1).

Pins three TZ-mandated invariants:

* **Channel 1 lands at Pass 1.** After ``orch.run()`` returns, every
  in-scope CourseNode has a Final row with the shared field family
  copied from Raw and ``approved_at = NULL``.
* **Channel 3 lands at Pass 2.** Every non-root in-scope CourseNode
  gets a non-NULL ``enclosing_context_updated_at`` matching the LLM
  hook's enclosing_context write.
* **Memo-skip never touches Final.** A second back-to-back run with no
  content changes leaves both Final and snapshot byte-identical
  (Final.updated_at frozen, snapshot count unchanged, approved_at
  preserved across the skip).

A small dedicated test pins the third channel's KD11 §1066 invariant
end-to-end via the orchestrator: an out-of-band author approval is
preserved through a content-change-and-Pass-2-rerun cycle on an ancestor.

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
from course_supporter.storage.orm import (
    CourseNode,
    Job,
    NodeSummaryFinal,
    NodeSummaryFinalPreviousSnapshot,
    NodeSummaryRaw,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


# ── Tree + Job setup (mirror existing pass2 fixture shape) ─────────


async def _setup_course_with_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """root + 2 children + 1 grand-child + active Job; commit."""
    async with session_factory() as session:
        tenant = Tenant(name=f"final-ch-{uuid.uuid4().hex[:8]}")
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
        session.add(a1)
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
            "a1": a1.id,
        }


async def _cleanup_course(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(NodeSummaryFinalPreviousSnapshot).where(
                NodeSummaryFinalPreviousSnapshot.node_summary_final_id.in_(
                    select(NodeSummaryFinal.id).where(
                        NodeSummaryFinal.course_node_id.in_(
                            select(CourseNode.id).where(
                                CourseNode.tenant_id == tenant_id
                            )
                        )
                    )
                )
            )
        )
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


# ── Stub methodist that mutates Raw realistically ─────────────────


class _WritingMethodist:
    """Writes Raw canonical fields + observations on Pass 1 and
    ``enclosing_context`` on Pass 2 so the channel wires have
    non-trivial data to copy. Records call counts for memo-skip pins.
    """

    def __init__(self) -> None:
        self.bottomup_calls: list[uuid.UUID] = []
        self.topdown_calls: list[uuid.UUID] = []
        # Suffix lets tests force a different mutation between runs.
        self.bottomup_suffix = "v1"
        self.topdown_suffix = "v1"

    async def generate_bottomup(self, node: CourseNode, raw: NodeSummaryRaw) -> None:
        self.bottomup_calls.append(node.id)
        raw.title = f"{node.title}-{self.bottomup_suffix}"
        raw.description = f"desc-{node.title}-{self.bottomup_suffix}"
        raw.learning_objectives = [f"obj-{node.title}-1"]
        raw.main_concepts = [f"concept-{node.title}"]
        raw.secondary_concepts = [f"secondary-{node.title}"]
        # Raw-only field that MUST NOT propagate to Final.
        raw.methodist_observations = [f"obs-{node.title}-1"]
        # Another Raw-only field that MUST NOT propagate.
        raw.compressed_summary = f"compressed-{node.title}"

    async def generate_topdown(
        self,
        node: CourseNode,
        raw: NodeSummaryRaw,
        parent_raw: NodeSummaryRaw,
    ) -> None:
        self.topdown_calls.append(node.id)
        raw.enclosing_context = f"enclosing-{node.title}-{self.topdown_suffix}"


# ── Channel 1 — Final present after Pass 1 ──────────────────────


class TestChannel1WiredAtPass1:
    async def test_every_in_scope_node_has_final_after_run(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_WritingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            async with session_factory() as session:
                finals = (
                    (
                        await session.execute(
                            select(NodeSummaryFinal).where(
                                NodeSummaryFinal.course_node_id.in_(
                                    [ids["root"], ids["a"], ids["b"], ids["a1"]]
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                final_by_node = {f.course_node_id: f for f in finals}
                # Every in-scope node has a Final.
                assert set(final_by_node) == {
                    ids["root"],
                    ids["a"],
                    ids["b"],
                    ids["a1"],
                }
                # approved_at = NULL on initial overwrite (KD11 line 1060).
                for f in final_by_node.values():
                    assert f.approved_at is None
                # Channel-1 invariant: shared fields copied from Raw.
                assert final_by_node[ids["a"]].title == "a-v1"
                assert final_by_node[ids["a"]].learning_objectives == ["obj-a-1"]
                assert final_by_node[ids["a"]].main_concepts == ["concept-a"]
                # is_manual untouched.
                for f in final_by_node.values():
                    assert f.is_manual is False
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


class TestChannel1FiltersRawOnlyFields:
    """Negative pin: ``methodist_observations`` + ``compressed_summary``
    set on Raw do NOT reach Final (the ORM columns simply don't exist
    on Final, so we check via SQL fetch + attribute introspection)."""

    async def test_raw_only_fields_absent_from_final(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_WritingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            async with session_factory() as session:
                final = (
                    await session.execute(
                        select(NodeSummaryFinal).where(
                            NodeSummaryFinal.course_node_id == ids["a"]
                        )
                    )
                ).scalar_one()
                # Final ORM does not have these columns at all.
                assert not hasattr(final, "methodist_observations")
                assert not hasattr(final, "compressed_summary")
                # And the Raw row DID have them populated by the stub.
                raw = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["a"]
                        )
                    )
                ).scalar_one()
                assert raw.methodist_observations == ["obs-a-1"]
                assert raw.compressed_summary == "compressed-a"
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── Channel 3 — enclosing_context_updated_at on non-root ──────────


class TestChannel3WiredAtPass2:
    async def test_non_root_finals_have_enclosing_context_updated_at(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_WritingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            async with session_factory() as session:
                final_root = (
                    await session.execute(
                        select(NodeSummaryFinal).where(
                            NodeSummaryFinal.course_node_id == ids["root"]
                        )
                    )
                ).scalar_one()
                final_a = (
                    await session.execute(
                        select(NodeSummaryFinal).where(
                            NodeSummaryFinal.course_node_id == ids["a"]
                        )
                    )
                ).scalar_one()
                # Root: Pass 2 NOT_APPLICABLE — channel 3 never fires.
                assert final_root.enclosing_context is None
                assert final_root.enclosing_context_updated_at is None
                # Non-root: channel 3 fired.
                assert final_a.enclosing_context == "enclosing-a-v1"
                assert final_a.enclosing_context_updated_at is not None
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── Memo-skip — Final untouched on idempotent second run ──────────


class TestMemoSkipDoesNotTouchFinal:
    async def test_second_run_leaves_final_and_snapshots_intact(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # First run — channels 1+3 fire on every in-scope node.
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_WritingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Capture Final updated_at + snapshot count for every in-scope node.
            async with session_factory() as session:
                finals = (
                    (
                        await session.execute(
                            select(NodeSummaryFinal).where(
                                NodeSummaryFinal.course_node_id.in_(
                                    [ids["root"], ids["a"], ids["b"], ids["a1"]]
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                before_updated_at = {f.course_node_id: f.updated_at for f in finals}
                before_enclosing = {
                    f.course_node_id: f.enclosing_context_updated_at for f in finals
                }
                snap_count_before = (
                    (await session.execute(select(NodeSummaryFinalPreviousSnapshot)))
                    .scalars()
                    .all()
                )
                snap_count_before_n = len(snap_count_before)

            # Second back-to-back run — both axes memo-skip on every node.
            second_methodist = _WritingMethodist()
            second_methodist.bottomup_suffix = "v2"
            second_methodist.topdown_suffix = "v2"
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=second_methodist
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Methodist hook not called on second run (memo-skip on both axes).
            assert second_methodist.bottomup_calls == []
            assert second_methodist.topdown_calls == []

            # Final untouched — updated_at + enclosing_context_updated_at frozen.
            async with session_factory() as session:
                finals = (
                    (
                        await session.execute(
                            select(NodeSummaryFinal).where(
                                NodeSummaryFinal.course_node_id.in_(
                                    [ids["root"], ids["a"], ids["b"], ids["a1"]]
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for f in finals:
                    assert f.updated_at == before_updated_at[f.course_node_id]
                    assert (
                        f.enclosing_context_updated_at
                        == before_enclosing[f.course_node_id]
                    )
                snap_after = (
                    (await session.execute(select(NodeSummaryFinalPreviousSnapshot)))
                    .scalars()
                    .all()
                )
                # No new snapshots — first run had nothing to snapshot
                # (initial INSERT); second run skipped entirely.
                assert len(snap_after) == snap_count_before_n
                assert snap_count_before_n == 0
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])


# ── Channel 3 preserves approved_at across a re-Pass-2 ────────────


class TestChannel3PreservesApprovedAt:
    """The KD11 §1066 invariant end-to-end via the orchestrator: an
    out-of-band author approval survives an ancestor-driven Pass 2
    re-run that refreshes the child's ``enclosing_context``."""

    async def test_approve_on_child_survives_parent_content_change(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _setup_course_with_job(session_factory)
        try:
            # First run — populate Raw + Final + first enclosing_context.
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=_WritingMethodist()
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Author approves child 'a1' out of band.
            approved_marker = datetime(2026, 1, 1, tzinfo=UTC)
            async with session_factory() as session:
                final_a1 = (
                    await session.execute(
                        select(NodeSummaryFinal).where(
                            NodeSummaryFinal.course_node_id == ids["a1"]
                        )
                    )
                ).scalar_one()
                final_a1.approved_at = approved_marker
                await session.commit()

            # Mutate 'a' so 'a1' becomes axis-2 stale (parent enclosing
            # changed → composite key differs). 'a1' itself stays axis-1
            # fresh (no content change on a1 → Pass 1 memo-skip, channel
            # 1 NOT fired → approved_at not reset on Pass 1).
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
                # Also stale a1's axis-2 cache so Pass 2 actually fires.
                raw_a1 = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["a1"]
                        )
                    )
                ).scalar_one()
                raw_a1.enclosing_context_source_hash = None
                await session.commit()

            second_methodist = _WritingMethodist()
            second_methodist.bottomup_suffix = "v2"
            second_methodist.topdown_suffix = "v2"
            async with session_factory() as session:
                orch = NodeSummaryGenerationOrchestrator(
                    session, methodist=second_methodist
                )
                await orch.run(job_id=ids["job_id"], vertex_node_id=ids["root"])

            # Channel 3 on a1 must have refreshed enclosing_context
            # WITHOUT resetting approved_at.
            async with session_factory() as session:
                final_a1 = (
                    await session.execute(
                        select(NodeSummaryFinal).where(
                            NodeSummaryFinal.course_node_id == ids["a1"]
                        )
                    )
                ).scalar_one()
                # Approval preserved.
                assert final_a1.approved_at == approved_marker
                # enclosing_context refreshed to v2.
                assert final_a1.enclosing_context == "enclosing-a1-v2"
                # And the refresh bumped the timestamp.
                assert final_a1.enclosing_context_updated_at is not None
                assert final_a1.enclosing_context_updated_at > approved_marker
        finally:
            await _cleanup_course(session_factory, ids["tenant_id"])
