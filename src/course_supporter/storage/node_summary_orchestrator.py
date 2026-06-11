"""Two-pass methodist generation orchestrator (vision §3 KD10, Phase 3.2.2).

Skeleton-first. Owns the deterministic bones of ``node_summary_regeneration``:
the downward walk shape, ensure-Raw, two-axis scope-validation, per-node
COMMIT, run-state on ``Job.stage_progress``, and the DI seam for the
LLM hook that Phase 3.2.3 will fill in. **LLM generation itself is
stubbed** here via :class:`MethodistGenerator` — the default no-op
implementation lets the skeleton walk a synthetic subtree end-to-end
without any model calls; Phase 3.2.3 wires the real methodist agent
against the same protocol.

Commits 1-3 ship scope-validation, run-state shape, the Pass 1 leg
(ensure-Raw, memo-skip, hook, source-hash materialise, per-node
COMMIT), and the Pass 2 leg (root-of-course exemption, parent-Raw
resolution with None-contract enforcement, composite-key memoization
via Phase 3.2.1, ``enclosing_context_source_hash`` materialise).
Commit 4 adds the full crash-resume integration coverage on top of
the per-node COMMIT atomicity already enforced here.

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
from pydantic import ValidationError
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
from course_supporter.storage.orm import CourseNode, Job, NodeSummaryRaw

logger = structlog.get_logger(__name__)


# Defensive cap on the course-tree walk-up depth in ``_collect_course_nodes``.
# Real courses are shallow (a handful of levels); the cap exists to surface
# accidentally-introduced cycles in future schema changes rather than to bound
# legitimate work. Mirrors ``_MAX_WALK_DEPTH`` in ``storage/content_hash.py``
# (same defensive intent, separate concern: content_hash walks the hash chain;
# this walks the CourseNode parent chain).
_MAX_COURSE_DEPTH = 25


class Pass2ParentRawMissingError(RuntimeError):
    """Pass 2 None-contract violation: non-root node lost its parent Raw.

    Vision §3 KD10 + the 3.2.1 service ``_fetch_parent_raw`` collapses
    three semantically different "parent Raw absent" cases into a
    single ``None`` return value (root / missing CourseNode / missing
    parent Raw). The orchestrator excludes the root case BEFORE asking
    (Pass 2 short-circuits on ``node.parent_id IS NULL`` →
    :attr:`NodeSummaryNodeStatus.NOT_APPLICABLE`), so a ``None`` from
    parent-Raw lookup at this point is **always** an invariant
    violation — fail loud per K2-analog for axis 2.

    Discrimination (Phase 3.2.2 commit 3): three orthogonal sub-cases,
    pinned to inline diagnostics via the two boolean discriminators:

    * **A1 — ordering violation (in-scope parent without Raw):**
      ``parent_course_node_present=True`` and ``parent_in_scope=True``.
      Pass 1 was supposed to ensure-Raw before Pass 2 touched a
      child. Implies a Pass 1 visit error on the parent (also in
      ``errors[]``) — operator inspects both and decides whether to
      reactivate.

    * **A2 — uncovered-stale bypassed by ``force``:**
      ``parent_course_node_present=True`` and ``parent_in_scope=False``.
      Vertex is not the course root and its parent (outside scope) has
      no Raw — ``validate_scope`` flagged this as ``uncovered_stale``;
      the user ran with ``force=True``. Expected when force is used
      with a small vertex.

    * **B — missing parent CourseNode:**
      ``parent_course_node_present=False``. The parent CourseNode is
      not in the orchestrator's in-memory course tree, either because
      a cascade soft-delete fired between scope-validation and the
      Pass 2 visit, or because ``node.parent_id`` points at a deleted
      / nonexistent CourseNode (structural inconsistency).

    Caught by :meth:`NodeSummaryGenerationOrchestrator._visit_pass2`;
    recorded into ``run_state.errors[]`` + ``pass2[node]=ERROR``; the
    walk **continues** (K2 contract). ``update_source_hash(None)`` is
    NEVER called (which would corrupt the second-axis cache).
    """

    def __init__(
        self,
        *,
        node_id: uuid.UUID,
        parent_course_node_id: uuid.UUID,
        parent_course_node_present: bool,
        parent_in_scope: bool,
    ) -> None:
        self.node_id = node_id
        self.parent_course_node_id = parent_course_node_id
        self.parent_course_node_present = parent_course_node_present
        self.parent_in_scope = parent_in_scope

        if not parent_course_node_present:
            sub_case = (
                "B (missing parent CourseNode — soft-delete race or "
                "structural inconsistency)"
            )
        elif parent_in_scope:
            sub_case = "A1 (in-scope parent without Raw — Pass 1 ordering violation)"
        else:
            sub_case = (
                "A2 (out-of-scope parent without Raw — uncovered_stale "
                "bypassed by force=True)"
            )
        super().__init__(
            f"Pass 2 None-contract violation on node {node_id}: {sub_case} "
            f"(parent_course_node_id={parent_course_node_id})"
        )


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

    Commit 3 ships the Pass 2 leg in addition to Pass 1 + scope
    validation: root-of-course → ``NOT_APPLICABLE`` short-circuit,
    parent-Raw resolution with None-contract enforcement
    (:class:`Pass2ParentRawMissingError`), composite-key memoization
    via the Phase 3.2.1 service, and per-node materialisation of
    ``enclosing_context_source_hash`` after the (no-op default) hook.
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

        Commits 2+3 scope:

        1. ``Job.current_stage='bottomup'`` + ``validate_scope`` +
           seed ``pass1`` PENDING + commit.
        2. Walk in-scope **leaf→root** for Pass 1
           (:meth:`_visit_pass1` — ensure-Raw / memo-skip / hook /
           materialise source_content_hash / per-node COMMIT).
        3. ``Job.current_stage='topdown'`` + seed ``pass2`` PENDING +
           commit.
        4. Walk in-scope **root→leaves** for Pass 2
           (:meth:`_visit_pass2` — root-of-course →
           ``NOT_APPLICABLE``; non-root → fetch parent_raw with
           None-contract enforcement, composite-key memo, hook,
           materialise ``enclosing_context_source_hash`` via the
           3.2.1 service, per-node COMMIT).

        ``force`` is informational here per K1 ratify (memo-skip is
        unconditional on both axes; force only changes the API's 422
        decision and is recorded into run-state for diagnostics).
        """
        vertex = await self._session.get(CourseNode, vertex_node_id)
        if vertex is None or vertex.deleted_at is not None:
            msg = f"run: vertex CourseNode {vertex_node_id} not found or soft-deleted"
            raise ValueError(msg)

        # Preserve cross-run errors[] per Q-4 contract (commit-1: ``errors``
        # is append-only ACROSS runs so resume sees the full attempt history).
        # Tolerant per-entry parse on read-back: ``extra='forbid'`` on the
        # top-level NodeSummaryRunState would refuse a future-version JSON
        # outright; per-entry ``NodeSummaryRunError.model_validate`` only
        # drops the offending entry (logged) and keeps the rest. This makes
        # forward-schema additions on the top-level safe for the resume path.
        preserved_errors = await self._read_back_preserved_errors(job_id)

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
            errors=preserved_errors,
        )
        await self._persist_run_state(job_id, run_state)
        await self._session.commit()

        course_nodes = await self._collect_course_nodes(vertex)
        in_scope_set = set(scope.in_scope_node_ids)
        pass1_order = self._compute_leaf_to_root_order(
            vertex_node_id,
            in_scope_set,
            course_nodes,
        )
        for nid in pass1_order:
            # K3 ratify: visits are STRICTLY sequential. Full-replace JSON
            # on Job.stage_progress is safe only because no two visits
            # race. Any future asyncio.gather over this loop = STOP-escalate.
            await self._visit_pass1(course_nodes[nid], run_state, job_id)

        # ── Pass 2 leg (commit 3) ──────────────────
        await job_repo.update_stage(job_id, "topdown")
        for nid in in_scope_set:
            run_state.pass2[nid] = NodeSummaryNodeStatus.PENDING
        run_state.updated_at = datetime.now(UTC)
        await self._persist_run_state(job_id, run_state)
        await self._session.commit()

        pass2_order = self._compute_root_to_leaf_order(
            vertex_node_id,
            in_scope_set,
            course_nodes,
        )
        for nid in pass2_order:
            # Same K3 sequential-only invariant as Pass 1.
            await self._visit_pass2(
                course_nodes[nid],
                run_state,
                job_id,
                course_nodes=course_nodes,
                in_scope_ids=in_scope_set,
            )

    # ─── helpers (private) ────────────────────────

    async def _collect_course_nodes(
        self, vertex: CourseNode
    ) -> dict[uuid.UUID, CourseNode]:
        """Load every active CourseNode in the course containing ``vertex``."""
        root = vertex
        depth = 0
        while root.parent_id is not None and depth < _MAX_COURSE_DEPTH:
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
        """Set-collect IDs under ``vertex_id`` using in-memory parent map.

        Iterative DFS with ``list.pop()`` (LIFO, O(1) from end). Returns
        a set; traversal order is irrelevant to the caller — the only
        invariant is "every descendant in the active tree is included".
        """
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

    @staticmethod
    def _compute_root_to_leaf_order(
        vertex_id: uuid.UUID,
        in_scope_ids: set[uuid.UUID],
        course_nodes: dict[uuid.UUID, CourseNode],
    ) -> list[uuid.UUID]:
        """Pre-order DFS from ``vertex_id`` over in-scope children.

        Mirror of :meth:`_compute_leaf_to_root_order` but emits the
        parent **before** its children — the correctness condition for
        the top-down Pass 2 (KD10 line 989: parents must materialise
        their ``enclosing_context`` before children read it for their
        own second-axis key). Iterative for the same depth-ceiling
        reason as the bottom-up sibling; children are pushed reversed
        so the pop order matches their natural insertion order.
        """
        children_by_parent: dict[uuid.UUID, list[uuid.UUID]] = {}
        for node_id in in_scope_ids:
            parent_id = course_nodes[node_id].parent_id
            if parent_id is not None and parent_id in in_scope_ids:
                children_by_parent.setdefault(parent_id, []).append(node_id)

        order: list[uuid.UUID] = []
        stack: list[uuid.UUID] = [vertex_id]
        seen: set[uuid.UUID] = set()
        while stack:
            node_id = stack.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            order.append(node_id)
            # Reverse so the natural-order child is popped first.
            for child_id in reversed(children_by_parent.get(node_id, [])):
                stack.append(child_id)
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

    async def _visit_pass2(
        self,
        node: CourseNode,
        run_state: NodeSummaryRunState,
        job_id: uuid.UUID,
        *,
        course_nodes: dict[uuid.UUID, CourseNode],
        in_scope_ids: set[uuid.UUID],
    ) -> None:
        """Per-node Pass 2 visit — atomic transaction (KD10 commit-per-node).

        Sequence within the single transaction:

        1. Course-root short-circuit: ``node.parent_id IS NULL`` →
           ``pass2[node]=NOT_APPLICABLE`` + commit; return. This is the
           **structural** Pass 2 exemption from KD10 line 1001 — applies
           only to the course root (not to the run's vertex when the
           vertex is an inner node, which is processed normally with
           its out-of-scope parent's Raw).
        2. Fetch own Raw (created by Pass 1). If missing → error path
           (Pass 1 prerequisite violation).
        3. Fetch parent Raw via direct lookup. ``None`` triggers
           :class:`Pass2ParentRawMissingError` with the A1/A2/B
           discrimination via in-memory ``course_nodes`` +
           ``in_scope_ids`` (Q-3b ratify); caught locally, recorded
           into ``run_state.errors[]`` + ``pass2[node]=ERROR``,
           ``update_source_hash(None)`` is NEVER called.
        4. Composite-key memo check via 3.2.1
           ``compute_source_hash_for(raw)``; equal to stored
           ``raw.enclosing_context_source_hash`` → ``SKIPPED_MEMO`` +
           commit; return. **Pass 2 skeleton does not write
           ``Final.enclosing_context_updated_at``** — the third
           channel that touches that timestamp lives in Phase 3.2.4
           (KD11 lines 1060-1066); acceptance #7 is closed
           by-construction here.
        5. Invoke ``MethodistGenerator.generate_topdown(node, raw,
           parent_raw)`` (no-op default in skeleton; Phase 3.2.3 fills
           it in). ``parent_raw`` is guaranteed non-None at this point
           by the discriminator above.
        6. Materialise ``raw.enclosing_context_source_hash`` via the
           3.2.1 service ``update_source_hash`` (mirror Pass 1 timing —
           write after the hook returns, even when the hook is no-op).
        7. ``DONE`` status + persist + commit.

        Error contract (K2 dual): same as Pass 1. Per-node error
        records into ``errors[]`` + ``pass2[node]=ERROR``, the walk
        continues. A child's Pass 2 visit may then see its parent in
        ERROR state (parent's enclosing_context unchanged from its
        previous value — possibly NULL on first ever run); skeleton's
        deterministic-key formula keeps the child's hash stable across
        runs in that case (parent.enclosing_context coalesces to "" in
        the 3.2.1 service).
        """
        # Step 1 — course-root structural exemption.
        if node.parent_id is None:
            run_state.pass2[node.id] = NodeSummaryNodeStatus.NOT_APPLICABLE
            run_state.updated_at = datetime.now(UTC)
            await self._persist_run_state(job_id, run_state)
            await self._session.commit()
            return

        # Step 2 — own Raw must exist (Pass 1 ensure-Raw).
        raw = await self._fetch_raw_for_node(node.id)
        if raw is None:
            run_state.errors.append(
                NodeSummaryRunError(
                    node_id=node.id,
                    stage="topdown",
                    reason=(
                        "Pass 2 prerequisite: own NodeSummaryRaw row is missing "
                        "(Pass 1 must run successfully before Pass 2 visits a node)"
                    ),
                )
            )
            run_state.pass2[node.id] = NodeSummaryNodeStatus.ERROR
            run_state.updated_at = datetime.now(UTC)
            await self._persist_run_state(job_id, run_state)
            await self._session.commit()
            return

        # Step 3 — fetch parent Raw with None-contract discrimination.
        parent_course_node_id = node.parent_id
        parent_raw = await self._fetch_raw_for_node(parent_course_node_id)
        if parent_raw is None:
            parent_present = parent_course_node_id in course_nodes
            parent_in_scope = parent_present and parent_course_node_id in in_scope_ids
            exc = Pass2ParentRawMissingError(
                node_id=node.id,
                parent_course_node_id=parent_course_node_id,
                parent_course_node_present=parent_present,
                parent_in_scope=parent_in_scope,
            )
            run_state.errors.append(
                NodeSummaryRunError(
                    node_id=node.id,
                    stage="topdown",
                    reason=str(exc),
                )
            )
            run_state.pass2[node.id] = NodeSummaryNodeStatus.ERROR
            run_state.updated_at = datetime.now(UTC)
            await self._persist_run_state(job_id, run_state)
            await self._session.commit()
            return

        # Step 4 — composite-key memo via 3.2.1.
        computed_hash = await self._encl_hash.compute_source_hash_for(raw)
        if computed_hash is None:  # pragma: no cover — 3.2.1 contract drift guard
            msg = (
                f"compute_source_hash_for returned None despite parent_raw "
                f"being present for node {node.id}"
            )
            raise RuntimeError(msg)

        if computed_hash == raw.enclosing_context_source_hash:
            run_state.pass2[node.id] = NodeSummaryNodeStatus.SKIPPED_MEMO
            run_state.updated_at = datetime.now(UTC)
            await self._persist_run_state(job_id, run_state)
            await self._session.commit()
            return

        # Step 5 — invoke the hook.
        try:
            await self._methodist.generate_topdown(node, raw, parent_raw)
        except Exception as exc:
            run_state.errors.append(
                NodeSummaryRunError(
                    node_id=node.id,
                    stage="topdown",
                    reason=str(exc),
                )
            )
            run_state.pass2[node.id] = NodeSummaryNodeStatus.ERROR
            run_state.updated_at = datetime.now(UTC)
            await self._persist_run_state(job_id, run_state)
            await self._session.commit()
            return

        # Step 6 — materialise via 3.2.1 (mirror Pass 1 timing).
        await self._encl_hash.update_source_hash(raw, computed_hash)

        # Step 7 — DONE.
        run_state.pass2[node.id] = NodeSummaryNodeStatus.DONE
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

    async def _read_back_preserved_errors(
        self, job_id: uuid.UUID
    ) -> list[NodeSummaryRunError]:
        """Read ``errors[]`` from existing ``stage_progress`` (Q-4 cross-run).

        Tolerant per-entry parse: bad entries are logged + skipped, good
        entries are preserved. NULL stage_progress / missing ``errors``
        key / non-list shape → empty list. Schema drift on the top-level
        does NOT lose the history (we never call ``from_jsonb`` on the
        outer envelope here).
        """
        existing_job = await self._session.get(Job, job_id)
        if existing_job is None or not isinstance(existing_job.stage_progress, dict):
            return []
        raw_errors = existing_job.stage_progress.get("errors", [])
        if not isinstance(raw_errors, list):
            return []
        preserved: list[NodeSummaryRunError] = []
        for entry in raw_errors:
            try:
                preserved.append(NodeSummaryRunError.model_validate(entry))
            except ValidationError:
                logger.warning(
                    "node_summary_run_state_error_entry_skipped",
                    job_id=str(job_id),
                )
        return preserved
