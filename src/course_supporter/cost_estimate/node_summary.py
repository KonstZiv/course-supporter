"""What a node-summary regeneration job will read, from the database (BIL-D).

Purpose:
    The estimate of a regeneration (``actions.estimate_node_summary_regeneration``)
    is pure; it needs to be told which nodes will call the model and how much
    each call reads. This module answers both from the same rows the
    orchestrator and the methodist agent read, **before** the job's first paid
    step, and never writes.

The rules it mirrors (each is checked against its source in the tests):

* A node calls the bottom-up model when its summary is stale on the first axis
  — no ``NodeSummaryRaw``, or ``source_content_hash`` differs from the node's
  ``content_hash`` (``NodeSummaryGenerationOrchestrator._is_axis1_fresh``).
* A non-root node calls the top-down model when its second-axis key is stale
  now (or it has no ``NodeSummaryRaw`` yet), and so does every non-root node
  below one that calls it: the parent's enclosing context is rewritten, which
  changes the key of its children. The key depends on the parent's enclosing
  context only, so a node's own bottom-up regeneration does not make its own
  top-down call.
* A bottom-up call reads its own READY documents — methodological documents,
  then tasks, in full while the full-text budget lasts, the rest as summaries
  — and each child's ``compressed_summary`` (``agents.methodist``:
  ``build_document_blocks``, ``_fetch_own_documents``, ``_fetch_children_raws``).

Interface:
    :func:`read_node_summary_factors` — the factors of one job.
    :func:`bottomup_chars` — the characters one bottom-up call reads.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.agents.methodist import (
    FULL_TEXT_BUDGET_CHARS,
    join_segment_texts,
)
from course_supporter.cost_estimate.actions import NodeSummaryFactors
from course_supporter.cost_estimate.settings import get_estimate_settings
from course_supporter.models.source import MaterialRole
from course_supporter.storage.enclosing_context_hash import (
    EnclosingContextHashService,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    NodeSummaryRaw,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class OwnDocumentSize:
    """The size of one READY own document of a node, as a bottom-up call reads it.

    ``entry_chars`` — title, description and concepts (every document is in the
    prompt at least as this); ``text_chars`` — the full text, read only for a
    methodological document or a task (0 when it has no active segments).
    """

    entry_chars: int
    text_chars: int
    methodological: bool
    task: bool


def bottomup_chars(
    documents: Sequence[OwnDocumentSize],
    *,
    children_chars: int,
    budget: int = FULL_TEXT_BUDGET_CHARS,
) -> int:
    """The characters one bottom-up call reads beside its prompt.

    Methodological documents claim the full-text budget first, then tasks, each
    in input order; a document goes in whole or as its summary, never cut. The
    same rule as ``agents.methodist.build_document_blocks``.
    """
    in_full_order = [i for i, d in enumerate(documents) if d.methodological] + [
        i for i, d in enumerate(documents) if d.task and not d.methodological
    ]
    remaining = budget
    full = 0
    for i in in_full_order:
        text = documents[i].text_chars
        if 0 < text <= remaining:
            full += text
            remaining -= text
    return sum(d.entry_chars for d in documents) + full + children_chars


async def _subtree(session: AsyncSession, vertex_id: uuid.UUID) -> list[CourseNode]:
    """The active nodes under the vertex, itself included (``UNION`` ends cycles)."""
    tree = (
        select(CourseNode.id)
        .where(CourseNode.id == vertex_id, CourseNode.deleted_at.is_(None))
        .cte("subtree", recursive=True)
    )
    tree = tree.union(
        select(CourseNode.id).where(
            CourseNode.parent_id == tree.c.id, CourseNode.deleted_at.is_(None)
        )
    )
    result = await session.execute(
        select(CourseNode).where(CourseNode.id.in_(select(tree.c.id)))
    )
    return list(result.scalars())


async def _raws(
    session: AsyncSession, node_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, NodeSummaryRaw]:
    result = await session.execute(
        select(NodeSummaryRaw).where(
            NodeSummaryRaw.course_node_id.in_(node_ids),
            NodeSummaryRaw.deleted_at.is_(None),
        )
    )
    return {r.course_node_id: r for r in result.scalars()}


def _is_axis1_fresh(node: CourseNode, raw: NodeSummaryRaw | None) -> bool:
    """The orchestrator's memo-skip test (``_is_axis1_fresh``), word for word."""
    return raw is not None and raw.source_content_hash == node.content_hash


async def _topdown_ids(
    session: AsyncSession,
    vertex_id: uuid.UUID,
    nodes: Mapping[uuid.UUID, CourseNode],
    raws: Mapping[uuid.UUID, NodeSummaryRaw],
) -> set[uuid.UUID]:
    """The nodes whose top-down pass will call the model (module rule)."""
    children: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for node in nodes.values():
        if node.parent_id is not None:
            children[node.parent_id].append(node.id)
    encl = EnclosingContextHashService(session)
    called: set[uuid.UUID] = set()
    frontier = [vertex_id]
    while frontier:  # parents before children
        next_frontier: list[uuid.UUID] = []
        for nid in frontier:
            node = nodes[nid]
            if node.parent_id is not None:
                raw = raws.get(nid)
                if node.parent_id in called or raw is None or await encl.is_stale(raw):
                    called.add(nid)
            next_frontier.extend(children.get(nid, ()))
        frontier = next_frontier
    return called


async def _document_sizes(
    session: AsyncSession, node_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[OwnDocumentSize]]:
    """The READY own documents of the nodes, in the author's order."""
    result = await session.execute(
        select(
            AuthoredDocument.course_node_id,
            AuthoredDocument.material_role,
            AuthoredDocument.task_type,
            AuthoredDocument.source_type,
            DocumentSummary.id,
            DocumentSummary.title,
            DocumentSummary.description,
            DocumentSummary.main_concepts,
            DocumentSummary.secondary_concepts,
        )
        .join(
            DocumentSummary,
            DocumentSummary.authored_document_id == AuthoredDocument.id,
        )
        .where(AuthoredDocument.course_node_id.in_(node_ids))
        .where(AuthoredDocument.deleted_at.is_(None))
        .where(AuthoredDocument.error_message.is_(None))
        .where(AuthoredDocument.job_id.is_(None))
        .where(DocumentSummary.deleted_at.is_(None))
        .order_by(
            AuthoredDocument.course_node_id,
            AuthoredDocument.order,
            AuthoredDocument.id,
        )
    )
    rows = list(result.all())
    in_full = [
        r.id
        for r in rows
        if r.material_role == MaterialRole.METHODOLOGICAL.value
        or r.task_type is not None
    ]
    text: dict[uuid.UUID, tuple[int, int]] = {}
    if in_full:
        segments = await session.execute(
            select(
                DocumentSegment.document_summary_id,
                func.coalesce(func.sum(func.length(DocumentSegment.content)), 0),
                func.count(),
            )
            .where(DocumentSegment.document_summary_id.in_(in_full))
            .where(DocumentSegment.deleted_at.is_(None))
            .group_by(DocumentSegment.document_summary_id)
        )
        text = {sid: (int(chars), int(count)) for sid, chars, count in segments}
    sizes: dict[uuid.UUID, list[OwnDocumentSize]] = defaultdict(list)
    for r in rows:
        chars, count = text.get(r.id, (0, 0))
        separator = len(join_segment_texts(["", ""], r.source_type))
        sizes[r.course_node_id].append(
            OwnDocumentSize(
                entry_chars=len(r.title or "")
                + len(r.description or "")
                + sum(len(c) for c in (r.main_concepts or []))
                + sum(len(c) for c in (r.secondary_concepts or [])),
                text_chars=chars + separator * max(count - 1, 0),
                methodological=r.material_role == MaterialRole.METHODOLOGICAL.value,
                task=r.task_type is not None,
            )
        )
    return sizes


async def read_node_summary_factors(
    session: AsyncSession,
    vertex_node_id: uuid.UUID,
    *,
    child_summary_chars: int | None = None,
) -> NodeSummaryFactors:
    """The factors of the regeneration job whose vertex is ``vertex_node_id``.

    ``child_summary_chars`` is what a child that has no summary yet is read at;
    ``None`` — the settings' (``processing.node_summary.child_summary_chars``),
    as the job and its price before the action read it. Read-only; the caller
    owns the session.
    """
    if child_summary_chars is None:
        child_summary_chars = (
            get_estimate_settings().processing.node_summary.child_summary_chars
        )
    nodes = {n.id: n for n in await _subtree(session, vertex_node_id)}
    if not nodes:
        return NodeSummaryFactors(bottomup_chars=(), topdown_nodes=0, nodes=0)
    raws = await _raws(session, list(nodes))
    stale = [n.id for n in nodes.values() if not _is_axis1_fresh(n, raws.get(n.id))]
    documents = await _document_sizes(session, stale)
    children: dict[uuid.UUID, list[CourseNode]] = defaultdict(list)
    for node in nodes.values():
        if node.parent_id is not None:
            children[node.parent_id].append(node)
    stale_set = set(stale)

    def children_chars(node_id: uuid.UUID) -> int:
        total = 0
        for child in children.get(node_id, ()):
            raw = raws.get(child.id)
            summary = (raw.compressed_summary if raw else None) or ""
            if not summary and child.id in stale_set:
                total += child_summary_chars  # it will have one by then
            total += len(child.title or "") + len(summary)
        return total

    return NodeSummaryFactors(
        bottomup_chars=tuple(
            bottomup_chars(documents.get(nid, ()), children_chars=children_chars(nid))
            for nid in stale
        ),
        topdown_nodes=len(await _topdown_ids(session, vertex_node_id, nodes, raws)),
        nodes=len(nodes),
    )
