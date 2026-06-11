"""Two-pass methodist generation orchestrator (vision §3 KD10, Phase 3.2.2).

Skeleton-first. Owns the deterministic bones of ``node_summary_regeneration``:
the downward walk shape, ensure-Raw, two-axis scope-validation, per-node
COMMIT, run-state on ``Job.stage_progress``, and the DI seam for the
LLM hook that Phase 3.2.3 will fill in. **LLM generation itself is
stubbed** here via :class:`MethodistGenerator` — the default no-op
implementation lets the skeleton walk a synthetic subtree end-to-end
without any model calls; Phase 3.2.3 wires the real methodist agent
against the same protocol.

Commits 1+2 ship scope-validation, run-state shape, and the full
Pass 1 leg (ensure-Raw, memo-skip, hook, source-hash materialise,
per-node COMMIT). ``visit_pass2`` and the Pass 2 leg of :meth:`run`
land in commit 3 and currently raise ``NotImplementedError``.

Boundary recap (Q-5 ratify): the worker entry, ARQ task function,
``enqueue_node_summary_regeneration`` helper, and HTTP trigger all live
in Phase 3.2.4. The orchestrator's entry point is the plain Python
:meth:`run` method, which operates within an **already-created**
``Job`` row of type ``node_summary_regeneration``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.enclosing_context_hash import (
    EnclosingContextHashService,
)
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.node_summary_run_state import (
    NodeSummaryNodeStatus,
    NodeSummaryRunError,
    NodeSummaryRunScope,
    NodeSummaryRunState,
)
from course_supporter.storage.orm import CourseNode, NodeSummaryRaw

logger = structlog.get_logger(__name__)


class MethodistGenerator(Protocol):
    """LLM-bearing hook for both methodist passes (vision §3 KD10).

    Phase 3.2.3 ships the concrete implementation backed by the
    methodist agent + ``ladders_methodist.yaml`` (stages
    ``methodist_bottomup`` / ``methodist_topdown``). Phase 3.2.2 ships
    only the no-op default so the orchestrator can drive its
    skeleton over a synthetic subtree without any LLM calls.

    Single-protocol decision (Q-3 ratify): the methodist is **one
    agent with two passes**, not two agents. Matching ladders_methodist
    yaml shape (one config, two stages). Both methods receive the
    Raw row as a **mutable** ORM object and write canonical content
    fields (Pass 1) or ``enclosing_context`` (Pass 2) directly onto
    it; the orchestrator owns flush + COMMIT + materialise of the
    two source-hash linkages.

    None-contract (orchestrator-enforced, hook never sees it): for
    a root node Pass 2 is **skipped entirely** by the orchestrator —
    the hook is not invoked, ``parent_raw`` is never ``None`` in
    real flow. For a non-root node with missing parent Raw, the
    orchestrator raises before calling the hook (run-state error +
    Pass 2 visit aborts). Hooks therefore receive a guaranteed-
    present ``parent_raw`` on every Pass 2 invocation.
    """

    async def generate_bottomup(self, node: CourseNode, raw: NodeSummaryRaw) -> None:
        """Write Pass 1 canonical fields + ``compressed_summary`` on ``raw``.

        Mutate ``raw`` in place; the orchestrator flushes/commits
        and materialises ``raw.source_content_hash`` after the hook
        returns. Re-raise any LLM/retry/structural error — the
        orchestrator catches and routes into run-state errors[].
        """

    async def generate_topdown(
        self,
        node: CourseNode,
        raw: NodeSummaryRaw,
        parent_raw: NodeSummaryRaw,
    ) -> None:
        """Write Pass 2 ``enclosing_context`` + observations on ``raw``.

        ``parent_raw`` is guaranteed non-None (root short-circuits in
        the orchestrator). Mutate ``raw`` in place; the orchestrator
        flushes/commits and materialises
        ``raw.enclosing_context_source_hash`` after the hook returns.
        """


class _NoOpMethodistGenerator:
    """Default :class:`MethodistGenerator` — does nothing (Phase 3.2.2 skeleton).

    Phase 3.2.3 replaces this with the real methodist agent via DI.
    With this stub installed the orchestrator's per-node COMMITs are
    "structural-only": Raw rows get materialised + ``source_content_hash``
    is recorded, but canonical content fields stay at their DB defaults
    (empty strings / empty lists). This keeps the resumability and
    memoization machinery testable end-to-end without any LLM calls.
    """

    async def generate_bottomup(self, node: CourseNode, raw: NodeSummaryRaw) -> None:
        return

    async def generate_topdown(
        self,
        node: CourseNode,
        raw: NodeSummaryRaw,
        parent_raw: NodeSummaryRaw,
    ) -> None:
        return


class NodeSummaryGenerationOrchestrator:
    """Two-pass methodist generation orchestrator (vision §3 KD10).

    Construct one per ``Job``; instances are stateless aside from the
    bound session and the injected :class:`MethodistGenerator`. The
    orchestrator does NOT create Job rows, enqueue ARQ tasks, or
    speak HTTP — Phase 3.2.4 owns all of that.

    Commit 2 ships scope-validation + Pass 1 leg (ensure-Raw +
    memo-skip + no-op hook + source-hash materialise + per-node
    COMMIT). Pass 2 leg + None-contract enforcement land in commit 3.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        methodist: MethodistGenerator | None = None,
    ) -> None:
        self._session = session
        self._methodist: MethodistGenerator = methodist or _NoOpMethodistGenerator()
        self._encl_hash = EnclosingContextHashService(session)

    async def validate_scope(
        self, vertex_node_id: uuid.UUID, force: bool
    ) -> NodeSummaryRunScope:
        """Classify all course CourseNodes by ``in_scope`` vs ``uncovered_stale``.

        Walks the entire course (rooted at ``vertex``'s root ancestor)
        and returns two disjoint sets:

        * ``in_scope_node_ids`` — every active CourseNode in the
          subtree under ``vertex`` (root inclusive); these are what
          Pass 1 + Pass 2 will visit. Memoization handles freshness
          optimization at visit time; scope itself is structural.
        * ``uncovered_stale_node_ids`` — stale CourseNodes elsewhere
          in the course (NOT in the vertex's subtree) that the
          current run will leave behind. Informational only — the
          run does **not** raise on this; the 422 surface is the
          API in Phase 3.2.4.

        Staleness is two-axis (vision §3 KD10 line 975):

        * Axis 1 — ``content_hash`` linkage. A CourseNode is axis-1
          stale when its Raw row is absent OR its
          ``raw.source_content_hash`` does not equal the live
          ``course_node.content_hash`` (KD10 line 1011 cache key).
        * Axis 2 — ``enclosing_context`` linkage. A non-root
          CourseNode whose Raw exists is axis-2 stale when the
          Phase 3.2.1 service reports
          ``EnclosingContextHashService.is_stale(raw)``.

        ``force`` is accepted for signature symmetry with the
        orchestrator's :meth:`run` and the 3.2.4 API; the
        classification itself is force-agnostic by design (force only
        affects whether the API raises 422 on
        ``uncovered_stale_node_ids``, not what classification the
        nodes receive).
        """
        del force  # informational at run/API level; not a classifier input

        vertex = await self._session.get(CourseNode, vertex_node_id)
        if vertex is None or vertex.deleted_at is not None:
            msg = (
                f"validate_scope: vertex CourseNode {vertex_node_id} "
                f"not found or soft-deleted"
            )
            raise ValueError(msg)

        course_nodes = await self._collect_course_nodes(vertex)
        subtree_ids = self._collect_subtree_ids(vertex.id, course_nodes)

        raws_by_node = await self._fetch_raws(list(course_nodes.keys()))

        stale_anywhere: set[uuid.UUID] = set()
        for node_id, node in course_nodes.items():
            raw = raws_by_node.get(node_id)
            if await self._is_node_stale(node, raw):
                stale_anywhere.add(node_id)

        uncovered = sorted(stale_anywhere - subtree_ids)
        in_scope = sorted(subtree_ids)

        return NodeSummaryRunScope(
            in_scope_node_ids=in_scope,
            uncovered_stale_node_ids=uncovered,
        )

    async def run(
        self,
        *,
        job_id: uuid.UUID,
        vertex_node_id: uuid.UUID,
        force: bool = False,
    ) -> None:
        """Orchestrate Pass 1 (Pass 2 lands in commit 3) on an existing Job row.

        Commit-2 scope:

        1. ``Job.current_stage='bottomup'``.
        2. ``validate_scope`` → record into ``run_state.scope``.
        3. Seed ``run_state.pass1[node]=pending`` for every in-scope node;
           persist + commit (so resume sees the initial scope even if
           a crash happens before the first visit lands).
        4. Walk in-scope leaf→root order; for each node call
           :meth:`_visit_pass1` (per-node atomic transaction:
           ensure-Raw → memo-skip OR hook → source-hash → status → commit).

        ``force`` is informational here per K1 ratify (memo-skip is
        unconditional; force only changes the API's 422 decision,
        recorded into run-state for resume diagnostics).
        """
        vertex = await self._session.get(CourseNode, vertex_node_id)
        if vertex is None or vertex.deleted_at is not None:
            msg = f"run: vertex CourseNode {vertex_node_id} not found or soft-deleted"
            raise ValueError(msg)

        job_repo = JobRepository(self._session)
        await job_repo.update_stage(job_id, "bottomup")

        scope = await self.validate_scope(vertex_node_id, force)
        run_state = NodeSummaryRunState(
            vertex_node_id=vertex_node_id,
            force=force,
            scope=scope,
            pass1={
                nid: NodeSummaryNodeStatus.PENDING for nid in scope.in_scope_node_ids
            },
        )
        await self._persist_run_state(job_id, run_state)
        await self._session.commit()

        course_nodes = await self._collect_course_nodes(vertex)
        order = self._compute_leaf_to_root_order(
            vertex_node_id,
            set(scope.in_scope_node_ids),
            course_nodes,
        )
        for nid in order:
            # K3 ratify: visits are STRICTLY sequential. Full-replace JSON
            # on Job.stage_progress is safe only because no two visits
            # race. Any future asyncio.gather over this loop = STOP-escalate.
            await self._visit_pass1(course_nodes[nid], run_state, job_id)

    # ─── helpers (private) ────────────────────────

    async def _collect_course_nodes(
        self, vertex: CourseNode
    ) -> dict[uuid.UUID, CourseNode]:
        """Load every active CourseNode in the course containing ``vertex``."""
        root = vertex
        depth = 0
        while root.parent_id is not None and depth < 25:
            parent = await self._session.get(CourseNode, root.parent_id)
            if parent is None or parent.deleted_at is not None:
                break
            root = parent
            depth += 1

        result = await self._session.execute(
            select(CourseNode).where(
                CourseNode.tenant_id == root.tenant_id,
                CourseNode.deleted_at.is_(None),
            )
        )
        by_id = {n.id: n for n in result.scalars().all()}
        by_id.setdefault(root.id, root)
        by_id.setdefault(vertex.id, vertex)
        return by_id

    @staticmethod
    def _collect_subtree_ids(
        vertex_id: uuid.UUID, course_nodes: dict[uuid.UUID, CourseNode]
    ) -> set[uuid.UUID]:
        """BFS-collect IDs under ``vertex_id`` using in-memory parent map."""
        children_by_parent: dict[uuid.UUID, list[uuid.UUID]] = {}
        for node_id, node in course_nodes.items():
            if node.parent_id is not None:
                children_by_parent.setdefault(node.parent_id, []).append(node_id)

        collected: set[uuid.UUID] = {vertex_id}
        frontier = [vertex_id]
        while frontier:
            current = frontier.pop()
            for child_id in children_by_parent.get(current, []):
                if child_id in collected:
                    continue
                collected.add(child_id)
                frontier.append(child_id)
        return collected

    async def _fetch_raws(
        self, course_node_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, NodeSummaryRaw]:
        """Load active Raw rows for the given CourseNode IDs."""
        if not course_node_ids:
            return {}
        result = await self._session.execute(
            select(NodeSummaryRaw).where(
                NodeSummaryRaw.course_node_id.in_(course_node_ids),
                NodeSummaryRaw.deleted_at.is_(None),
            )
        )
        return {r.course_node_id: r for r in result.scalars().all()}

    async def _is_node_stale(
        self, node: CourseNode, raw: NodeSummaryRaw | None
    ) -> bool:
        """Two-axis staleness check (vision §3 KD10 line 975).

        Axis-1 negation delegates to :meth:`_is_axis1_fresh` so the
        memo-skip predicate in :meth:`_visit_pass1` and the scope-
        validation predicate here share one formula (single source of
        truth — no risk of the two drifting on a future tweak).
        """
        if not self._is_axis1_fresh(node, raw):
            return True
        # Past the early return, raw is non-None and axis-1 fresh.
        if node.parent_id is None:
            return False
        assert raw is not None
        return await self._encl_hash.is_stale(raw)

    @staticmethod
    def _is_axis1_fresh(node: CourseNode, raw: NodeSummaryRaw | None) -> bool:
        """Axis-1 (``content_hash`` linkage) freshness — KD10 line 1011.

        Shared by :meth:`_is_node_stale` (scope-validation) and
        :meth:`_visit_pass1` (memo-skip). Both read the same column
        with the same comparator; centralising the formula eliminates
        the drift risk between the two call-sites.
        """
        return raw is not None and raw.source_content_hash == node.content_hash

    async def _ensure_raw(self, course_node_id: uuid.UUID) -> NodeSummaryRaw:
        """Idempotent ensure-Raw — race-free at the DB level.

        Uses ``INSERT ... ON CONFLICT (course_node_id) DO NOTHING`` via
        the PG dialect so a parallel orchestrator (e.g. two operators
        re-running on overlapping subtrees) cannot collide on the
        ``UNIQUE`` constraint mid-flight. ``source_content_hash`` stays
        NULL on insert — only :meth:`_visit_pass1` materialises it
        (after the LLM hook commits per-node, even when the hook is the
        no-op default in skeleton 3.2.2).

        Returns the live Raw row (newly inserted or pre-existing).
        Structural fields land at their server-side defaults (empty
        strings / ``'[]'::jsonb`` / zero counters / empty-hash literal
        on ``content_hash``). Canonical content fields stay at their
        defaults until the LLM hook populates them in commit-2/3 +
        Phase 3.2.3.
        """
        stmt = (
            pg_insert(NodeSummaryRaw)
            .values(course_node_id=course_node_id)
            .on_conflict_do_nothing(index_elements=["course_node_id"])
        )
        await self._session.execute(stmt)
        await self._session.flush()
        result = await self._session.execute(
            select(NodeSummaryRaw).where(
                NodeSummaryRaw.course_node_id == course_node_id,
                NodeSummaryRaw.deleted_at.is_(None),
            )
        )
        raw = result.scalar_one_or_none()
        if raw is None:  # pragma: no cover — implies cascade soft-delete race
            msg = (
                f"_ensure_raw: NodeSummaryRaw for course_node {course_node_id} "
                f"is missing after INSERT — likely a concurrent cascade "
                f"soft-delete on the parent CourseNode"
            )
            raise RuntimeError(msg)
        return raw

    @staticmethod
    def _compute_leaf_to_root_order(
        vertex_id: uuid.UUID,
        in_scope_ids: set[uuid.UUID],
        course_nodes: dict[uuid.UUID, CourseNode],
    ) -> list[uuid.UUID]:
        """Post-order DFS from ``vertex_id`` over in-scope children.

        Iterative implementation (explicit stack of ``(node_id, processed)``
        tuples) to keep the recursion-depth ceiling on the Python stack
        out of the picture for pathologically deep courses. Course trees
        are shallow in practice (<10 levels), but the post-order DFS
        guarantee — every child emitted before its parent — is the
        correctness condition for the bottom-up pass (KD10 line 1024:
        children must be committed before the parent's visit starts).
        """
        children_by_parent: dict[uuid.UUID, list[uuid.UUID]] = {}
        for node_id in in_scope_ids:
            parent_id = course_nodes[node_id].parent_id
            if parent_id is not None and parent_id in in_scope_ids:
                children_by_parent.setdefault(parent_id, []).append(node_id)

        order: list[uuid.UUID] = []
        stack: list[tuple[uuid.UUID, bool]] = [(vertex_id, False)]
        seen: set[uuid.UUID] = set()
        while stack:
            node_id, processed = stack.pop()
            if processed:
                order.append(node_id)
                continue
            if node_id in seen:
                continue
            seen.add(node_id)
            stack.append((node_id, True))
            for child_id in children_by_parent.get(node_id, []):
                stack.append((child_id, False))
        return order

    async def _visit_pass1(
        self,
        node: CourseNode,
        run_state: NodeSummaryRunState,
        job_id: uuid.UUID,
    ) -> None:
        """Per-node Pass 1 visit — atomic transaction (KD10 commit-per-node).

        Sequence within the single transaction:

        1. ensure-Raw (idempotent INSERT ON CONFLICT DO NOTHING).
        2. fetch the live Raw row.
        3. memo-skip check (:meth:`_is_axis1_fresh`) — **unconditional**
           per K1 ratify; ``force`` does not regenerate axis-1-fresh
           nodes (force only widens scope at validate-time / API-time).
        4. invoke ``MethodistGenerator.generate_bottomup`` (no-op default
           in skeleton; Phase 3.2.3 fills it in).
        5. materialise ``raw.source_content_hash := node.content_hash``
           **after** the hook returns (even when the hook is no-op).
        6. record status in ``run_state.pass1[node.id]``.
        7. persist run-state + ``await self._session.commit()``.

        Error contract (K2 ratify): an exception inside the hook is
        recorded into ``run_state.errors[]`` + ``pass1[node]=error`` and
        committed; the walk **does not abort**. The default no-op hook
        cannot raise; this branch protects future Phase 3.2.3 LLM hooks
        + lets resume diagnostics see the full history. An error on a
        child leaves the parent with incomplete bottom-up input — the
        downstream propagation policy (whether the parent should still
        attempt generation, mark itself blocked, etc.) is **not**
        decided here; operators inspect errors[] after the run.
        """
        await self._ensure_raw(node.id)
        raw = await self._fetch_raw_for_node(node.id)
        if raw is None:  # pragma: no cover — _ensure_raw guarantees presence
            msg = f"_visit_pass1: Raw row vanished between ensure and fetch ({node.id})"
            raise RuntimeError(msg)

        if self._is_axis1_fresh(node, raw):
            run_state.pass1[node.id] = NodeSummaryNodeStatus.SKIPPED_MEMO
            run_state.updated_at = datetime.now(UTC)
            await self._persist_run_state(job_id, run_state)
            await self._session.commit()
            return

        try:
            await self._methodist.generate_bottomup(node, raw)
        except Exception as exc:
            run_state.errors.append(
                NodeSummaryRunError(
                    node_id=node.id,
                    stage="bottomup",
                    reason=str(exc),
                )
            )
            run_state.pass1[node.id] = NodeSummaryNodeStatus.ERROR
            run_state.updated_at = datetime.now(UTC)
            await self._persist_run_state(job_id, run_state)
            await self._session.commit()
            return

        raw.source_content_hash = node.content_hash
        run_state.pass1[node.id] = NodeSummaryNodeStatus.DONE
        run_state.updated_at = datetime.now(UTC)
        await self._persist_run_state(job_id, run_state)
        await self._session.commit()

    async def _fetch_raw_for_node(
        self, course_node_id: uuid.UUID
    ) -> NodeSummaryRaw | None:
        """Load the active Raw row for a single CourseNode (1:1)."""
        result = await self._session.execute(
            select(NodeSummaryRaw).where(
                NodeSummaryRaw.course_node_id == course_node_id,
                NodeSummaryRaw.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def _persist_run_state(
        self, job_id: uuid.UUID, run_state: NodeSummaryRunState
    ) -> None:
        """Full-replace ``Job.stage_progress`` from ``run_state`` (K3 contract)."""
        job_repo = JobRepository(self._session)
        await job_repo.update_stage_progress(job_id, run_state.to_jsonb())
