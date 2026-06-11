"""Two-pass methodist generation orchestrator (vision §3 KD10, Phase 3.2.2).

Skeleton-first. Owns the deterministic bones of ``node_summary_regeneration``:
the downward walk shape, ensure-Raw, two-axis scope-validation, per-node
COMMIT, run-state on ``Job.stage_progress``, and the DI seam for the
LLM hook that Phase 3.2.3 will fill in. **LLM generation itself is
stubbed** here via :class:`MethodistGenerator` — the default no-op
implementation lets the skeleton walk a synthetic subtree end-to-end
without any model calls; Phase 3.2.3 wires the real methodist agent
against the same protocol.

This module is **commit 1** of the four-commit Phase 3.2.2 plan: scope-
validation + run-state shape only. ``ensure_raw`` / ``visit_pass1`` /
``visit_pass2`` / ``run`` land in subsequent commits and raise
``NotImplementedError`` here so the import surface is stable but
incomplete execution is loud.

Boundary recap (Q-5 ratify): the worker entry, ARQ task function,
``enqueue_node_summary_regeneration`` helper, and HTTP trigger all live
in Phase 3.2.4. The orchestrator's entry point is the plain Python
:meth:`run` method, which operates within an **already-created**
``Job`` row of type ``node_summary_regeneration``.
"""

from __future__ import annotations

import uuid
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.enclosing_context_hash import (
    EnclosingContextHashService,
)
from course_supporter.storage.node_summary_run_state import NodeSummaryRunScope
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

    Skeleton commit 1 ships only :meth:`validate_scope`; ensure-Raw
    + Pass 1 + Pass 2 + :meth:`run` land in subsequent commits of
    the same task.
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
        """Orchestrate Pass 1 + Pass 2 on the existing ``Job`` row.

        Lands in commits 2-3 of Phase 3.2.2.
        """
        del job_id, vertex_node_id, force
        msg = "run() lands in Phase 3.2.2 commits 2-3 (Pass 1 + Pass 2 visits)"
        raise NotImplementedError(msg)

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
        """Two-axis staleness check (vision §3 KD10 line 975)."""
        if raw is None:
            return True
        if raw.source_content_hash != node.content_hash:
            return True
        if node.parent_id is None:
            return False
        return await self._encl_hash.is_stale(raw)
