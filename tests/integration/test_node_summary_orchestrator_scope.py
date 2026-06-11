"""Integration: ``validate_scope`` from Phase 3.2.2 orchestrator (commit 1).

Pins the two-axis scope-validation logic from vision §3 KD10 line 975
against a real PostgreSQL schema (so the new
``node_summaries_raw.source_content_hash`` column from
``phase322_node_summary_raw_source_hash`` is exercised end-to-end).

Acceptance scope for commit 1:

* in-scope = subtree of vertex (memo-skip is a visit-time concern,
  not a scope-time concern).
* uncovered-stale = stale CourseNodes anywhere in the course but
  outside vertex's subtree.
* axis-1 staleness fires on missing Raw OR mismatched
  ``source_content_hash``.
* axis-2 staleness fires on a non-root Raw whose
  ``enclosing_context_source_hash`` differs from the live parent's
  ``enclosing_context``.
* ``force`` is informational at validate_scope level (same return
  shape regardless of value).
* soft-deleted vertex → ``ValueError`` (caller is responsible for
  surfacing this as 404 at the 3.2.4 API).

Full Pass 1/Pass 2 acceptance + per-node COMMIT + resume tests land
in commits 2-4 of the same task.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.content_hash import (
    EMPTY_NODE_CONTENT_HASH,
)
from course_supporter.storage.enclosing_context_hash import (
    EnclosingContextHashService,
)
from course_supporter.storage.node_summary_orchestrator import (
    NodeSummaryGenerationOrchestrator,
)
from course_supporter.storage.orm import CourseNode, NodeSummaryRaw, Tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


# ── Tree builder ─────────────────────────────────────────────────


async def _build_course(
    session: AsyncSession,
) -> dict[str, CourseNode]:
    """Build a deterministic 7-node tree:

    root
    ├── a
    │   ├── a1
    │   └── a2
    ├── b
    │   └── b1
    └── c
    """
    tenant = Tenant(name=f"orch-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()

    root = make_root_course_node(tenant_id=tenant.id, title="root", order=0)
    session.add(root)
    await session.flush()

    nodes: dict[str, CourseNode] = {"root": root}
    layout = [
        ("a", root.id, 0),
        ("a1", None, 0),
        ("a2", None, 1),
        ("b", root.id, 1),
        ("b1", None, 0),
        ("c", root.id, 2),
    ]
    placeholder: dict[str, uuid.UUID] = {}
    for name, parent_id, order in layout:
        if name in {"a1", "a2"}:
            parent_id = placeholder["a"]
        elif name == "b1":
            parent_id = placeholder["b"]
        node = CourseNode(
            tenant_id=tenant.id,
            parent_id=parent_id,
            title=name,
            order=order,
        )
        session.add(node)
        await session.flush()
        nodes[name] = node
        placeholder[name] = node.id

    return nodes


async def _make_raw(
    session: AsyncSession,
    *,
    course_node_id: uuid.UUID,
    source_content_hash: str | None = None,
    enclosing_context: str | None = None,
    enclosing_context_source_hash: str | None = None,
) -> NodeSummaryRaw:
    raw = NodeSummaryRaw(
        course_node_id=course_node_id,
        source_content_hash=source_content_hash,
        enclosing_context=enclosing_context,
        enclosing_context_source_hash=enclosing_context_source_hash,
    )
    session.add(raw)
    await session.flush()
    return raw


# ── Structural in-scope = subtree of vertex ──────────────────────


class TestInScopeIsSubtreeOfVertex:
    """Structural ``in_scope`` is the subtree under ``vertex``."""

    async def test_root_vertex_covers_entire_course(
        self, db_session: AsyncSession
    ) -> None:
        nodes = await _build_course(db_session)
        orch = NodeSummaryGenerationOrchestrator(db_session)
        scope = await orch.validate_scope(nodes["root"].id, force=False)
        expected = {n.id for n in nodes.values()}
        assert set(scope.in_scope_node_ids) == expected

    async def test_inner_vertex_covers_only_its_subtree(
        self, db_session: AsyncSession
    ) -> None:
        nodes = await _build_course(db_session)
        orch = NodeSummaryGenerationOrchestrator(db_session)
        scope = await orch.validate_scope(nodes["a"].id, force=False)
        assert set(scope.in_scope_node_ids) == {
            nodes["a"].id,
            nodes["a1"].id,
            nodes["a2"].id,
        }


# ── Axis 1 — content_hash linkage ────────────────────────────────


class TestAxisOneStaleness:
    """Raw missing OR ``source_content_hash`` mismatch → axis-1 stale."""

    async def test_all_nodes_stale_when_no_raws(self, db_session: AsyncSession) -> None:
        nodes = await _build_course(db_session)
        orch = NodeSummaryGenerationOrchestrator(db_session)
        # Vertex = subtree "a"; uncovered = everything else (root, b, b1, c).
        scope = await orch.validate_scope(nodes["a"].id, force=False)
        expected_uncovered = {
            nodes["root"].id,
            nodes["b"].id,
            nodes["b1"].id,
            nodes["c"].id,
        }
        assert set(scope.uncovered_stale_node_ids) == expected_uncovered

    async def test_matching_source_hash_clears_axis1(
        self, db_session: AsyncSession
    ) -> None:
        # Axis-1 alone is fresh; axis-2 still marks non-roots stale
        # because their ``enclosing_context_source_hash`` is NULL while
        # the parent's implicit ``enclosing_context or ""`` hashes to a
        # real digest. Root short-circuits axis-2 (no parent).
        # → uncovered_stale = {non-root outside vertex} = {b, b1, c}.
        nodes = await _build_course(db_session)
        for node in nodes.values():
            await _make_raw(
                db_session,
                course_node_id=node.id,
                source_content_hash=node.content_hash,
            )
        orch = NodeSummaryGenerationOrchestrator(db_session)
        scope = await orch.validate_scope(nodes["a"].id, force=False)
        # Root excluded (axis-2 N/A); a/a1/a2 in vertex's subtree.
        assert set(scope.uncovered_stale_node_ids) == {
            nodes["b"].id,
            nodes["b1"].id,
            nodes["c"].id,
        }
        assert nodes["root"].id not in scope.uncovered_stale_node_ids

    async def test_mismatched_source_hash_marks_stale(
        self, db_session: AsyncSession
    ) -> None:
        nodes = await _build_course(db_session)
        # Root + a + a1 + a2 — fresh; b/b1/c — wrong hash (axis-1 stale).
        for name in ("root", "a", "a1", "a2"):
            await _make_raw(
                db_session,
                course_node_id=nodes[name].id,
                source_content_hash=nodes[name].content_hash,
            )
        wrong_hash = "f" * 64
        for name in ("b", "b1", "c"):
            await _make_raw(
                db_session,
                course_node_id=nodes[name].id,
                source_content_hash=wrong_hash,
            )

        orch = NodeSummaryGenerationOrchestrator(db_session)
        scope = await orch.validate_scope(nodes["a"].id, force=False)
        assert set(scope.uncovered_stale_node_ids) == {
            nodes["b"].id,
            nodes["b1"].id,
            nodes["c"].id,
        }


# ── Axis 2 — enclosing_context linkage ───────────────────────────


class TestAxisTwoStaleness:
    """Non-root Raw with mismatched
    ``enclosing_context_source_hash`` → axis-2 stale.
    """

    async def test_axis2_stale_when_parent_context_drifts(
        self, db_session: AsyncSession
    ) -> None:
        nodes = await _build_course(db_session)
        # Every node has a Raw with matching axis-1 hash (so axis-1 is clean).
        # Parents carry a non-trivial enclosing_context, but the child's
        # enclosing_context_source_hash is stale (None) → axis-2 stale.
        for node in nodes.values():
            await _make_raw(
                db_session,
                course_node_id=node.id,
                source_content_hash=node.content_hash,
                enclosing_context="parent context payload",
            )

        orch = NodeSummaryGenerationOrchestrator(db_session)
        # Vertex = "a" (inner); uncovered = nodes outside subtree that are
        # axis-2-stale. b1 has a parent (b) whose enclosing_context is
        # non-empty, and b1's enclosing_context_source_hash is NULL ≠
        # the parent's hash → stale. Same for any other non-root.
        scope = await orch.validate_scope(nodes["a"].id, force=False)
        # Root has no parent (axis-2 N/A — never stale by axis 2).
        # b1 / c — non-root with NULL source_hash, parent has context → stale.
        # b is non-root child of root; same reasoning → stale.
        assert nodes["b"].id in scope.uncovered_stale_node_ids
        assert nodes["b1"].id in scope.uncovered_stale_node_ids
        assert nodes["c"].id in scope.uncovered_stale_node_ids
        assert nodes["root"].id not in scope.uncovered_stale_node_ids

    async def test_axis2_fresh_when_source_hash_matches(
        self, db_session: AsyncSession
    ) -> None:
        nodes = await _build_course(db_session)
        # Every parent's enclosing_context is the empty string; children get
        # the corresponding computed source-hash → axis-2 fresh.
        for node in nodes.values():
            await _make_raw(
                db_session,
                course_node_id=node.id,
                source_content_hash=node.content_hash,
                enclosing_context="",
            )
        # Now write each child's enclosing_context_source_hash from the
        # 3.2.1 service so axis-2 is in steady state.
        encl = EnclosingContextHashService(db_session)
        for name in ("a", "a1", "a2", "b", "b1", "c"):
            raw_row = (
                await db_session.execute(
                    select(NodeSummaryRaw).where(
                        NodeSummaryRaw.course_node_id == nodes[name].id
                    )
                )
            ).scalar_one()
            computed = await encl.compute_source_hash_for(raw_row)
            await encl.update_source_hash(raw_row, computed)

        orch = NodeSummaryGenerationOrchestrator(db_session)
        scope = await orch.validate_scope(nodes["root"].id, force=False)
        assert scope.uncovered_stale_node_ids == []


# ── force semantics + error cases ────────────────────────────────


class TestForceAndErrorHandling:
    """``force`` is informational at this layer; soft-delete raises."""

    async def test_force_does_not_change_classification(
        self, db_session: AsyncSession
    ) -> None:
        nodes = await _build_course(db_session)
        orch = NodeSummaryGenerationOrchestrator(db_session)
        a = await orch.validate_scope(nodes["a"].id, force=False)
        b = await orch.validate_scope(nodes["a"].id, force=True)
        assert a.in_scope_node_ids == b.in_scope_node_ids
        assert a.uncovered_stale_node_ids == b.uncovered_stale_node_ids

    async def test_soft_deleted_vertex_raises(self, db_session: AsyncSession) -> None:
        nodes = await _build_course(db_session)
        nodes["a"].deleted_at = datetime.now(UTC)
        await db_session.flush()
        orch = NodeSummaryGenerationOrchestrator(db_session)
        with pytest.raises(ValueError, match="not found or soft-deleted"):
            await orch.validate_scope(nodes["a"].id, force=False)

    async def test_missing_vertex_raises(self, db_session: AsyncSession) -> None:
        orch = NodeSummaryGenerationOrchestrator(db_session)
        with pytest.raises(ValueError, match="not found or soft-deleted"):
            await orch.validate_scope(uuid.uuid4(), force=False)


# ── content_hash sanity check on empty CourseNode ────────────────


class TestEmptyContentHashRecorded:
    """Sanity: freshly-INSERTed CourseNode carries the empty-content-hash literal."""

    async def test_fresh_root_has_empty_node_content_hash(
        self, db_session: AsyncSession
    ) -> None:
        tenant = Tenant(name=f"orch-empty-{uuid.uuid4().hex[:8]}")
        db_session.add(tenant)
        await db_session.flush()
        root = make_root_course_node(tenant_id=tenant.id, title="r", order=0)
        db_session.add(root)
        await db_session.flush()
        assert root.content_hash == EMPTY_NODE_CONTENT_HASH
