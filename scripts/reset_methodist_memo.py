"""Make the next methodist run regenerate a subtree after a prompt change.

The node-summary orchestrator skips a node whose ``NodeSummaryRaw
.source_content_hash`` equals the node's ``CourseNode.content_hash``, and
``force`` does not change that (``storage/node_summary_orchestrator.py``,
``_is_axis1_fresh``). The hash covers the materials only, so a new methodist
prompt alone never reaches a node whose materials did not change. The
top-down pass has its own key, ``enclosing_context_source_hash`` (the hash of
the parent's enclosing context), which a prompt change does not move either.
This script clears both keys on the Raw rows of one subtree; the next
"Generate description" run on that node (``POST /nodes/{node_id}/summary/generate``)
then regenerates every node of it.

It generates nothing, calls no model and touches nothing but those two
columns.

Consequences of the next run, per regenerated node:

* a new ``NodeSummaryRaw`` replaces the old one;
* the ``NodeSummaryFinal`` is overwritten from the new Raw and its approval is
  reset (``approved_at = NULL``); the previous Final is kept as a snapshot
  (``NodeSummaryFinalRepository.write_from_raw_with_snapshot``), so the author
  can compare and has to approve the description again;
* nodes outside the subtree are not regenerated: to refresh the parents with
  the children's new summaries, pass a higher node (the course root refreshes
  the whole course).

Usage::

    uv run python -m scripts.reset_methodist_memo <node-id>           # preview
    uv run python -m scripts.reset_methodist_memo <node-id> --apply   # reset

Without ``--apply`` the script only prints what it would reset.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from course_supporter.config import settings
from course_supporter.storage.orm import CourseNode, NodeSummaryRaw

_ROOT_WALK_CAP = 25
"""Depth cap on the walk to the course root (the methodist agent's own cap)."""


class ResetError(Exception):
    """The requested node cannot be reset; the message says why."""


@dataclass(frozen=True)
class ResetPlan:
    """What a reset of one subtree covers.

    Attributes:
        course_root: The root of the course the node belongs to.
        nodes: Every active node of the subtree, the requested node first.
        raws: The active Raw rows of those nodes, by node id.
    """

    course_root: CourseNode
    nodes: list[CourseNode]
    raws: dict[uuid.UUID, NodeSummaryRaw]

    @property
    def to_reset(self) -> list[NodeSummaryRaw]:
        """Raw rows with a memo key set — the ones a reset changes."""
        return [
            raw
            for n in self.nodes
            if (raw := self.raws.get(n.id)) is not None
            and (
                raw.source_content_hash is not None
                or raw.enclosing_context_source_hash is not None
            )
        ]


def get_sync_session() -> Session:
    """Sync session on the app's database (psycopg v3 serves both modes)."""
    return Session(create_engine(settings.database_url))


def plan_reset(session: Session, node_id: uuid.UUID) -> ResetPlan:
    """Collect the subtree of ``node_id`` and check it is one course.

    Raises:
        ResetError: The node does not exist or is deleted, the walk up does
            not reach a course root, or a node of the subtree belongs to
            another tenant than the course root.
    """
    node = session.get(CourseNode, node_id)
    if node is None or node.deleted_at is not None:
        raise ResetError(f"Node not found or deleted: {node_id}")

    root = node
    for _ in range(_ROOT_WALK_CAP):
        if root.parent_id is None:
            break
        parent = session.get(CourseNode, root.parent_id)
        if parent is None or parent.deleted_at is not None:
            raise ResetError(
                f"Node {root.id} has a missing or deleted parent {root.parent_id}"
            )
        root = parent
    if root.parent_id is not None:
        raise ResetError(f"No course root within {_ROOT_WALK_CAP} steps of {node_id}")

    nodes = [node]
    frontier = [node.id]
    while frontier:
        children = list(
            session.execute(
                select(CourseNode)
                .where(CourseNode.parent_id.in_(frontier))
                .where(CourseNode.deleted_at.is_(None))
                .order_by(CourseNode.order, CourseNode.id)
            ).scalars()
        )
        nodes.extend(children)
        frontier = [c.id for c in children]

    foreign = [n.id for n in nodes if n.tenant_id != root.tenant_id]
    if foreign:
        raise ResetError(
            f"Nodes of another tenant in the subtree of {node_id}: {foreign}"
        )

    raws = {
        r.course_node_id: r
        for r in session.execute(
            select(NodeSummaryRaw)
            .where(NodeSummaryRaw.course_node_id.in_([n.id for n in nodes]))
            .where(NodeSummaryRaw.deleted_at.is_(None))
        ).scalars()
    }
    return ResetPlan(course_root=root, nodes=nodes, raws=raws)


def apply_reset(session: Session, plan: ResetPlan) -> int:
    """Clear both memo keys on the plan's Raw rows; commit.

    Returns:
        The number of Raw rows changed.
    """
    raw_ids = [r.id for r in plan.to_reset]
    if not raw_ids:
        return 0
    session.execute(
        update(NodeSummaryRaw)
        .where(NodeSummaryRaw.id.in_(raw_ids))
        .values(source_content_hash=None, enclosing_context_source_hash=None)
    )
    session.commit()
    return len(raw_ids)


def describe(plan: ResetPlan) -> list[str]:
    """Human-readable lines: the course, then one line per subtree node."""
    lines = [
        f"Course: {plan.course_root.title} ({plan.course_root.id})",
        f"Subtree nodes: {len(plan.nodes)}, Raw rows to reset: {len(plan.to_reset)}",
    ]
    for node in plan.nodes:
        raw = plan.raws.get(node.id)
        if raw is None:
            state = "no Raw yet (generated on the next run anyway)"
        elif raw in plan.to_reset:
            state = "reset"
        else:
            state = "already reset"
        lines.append(f"  {node.id}  {node.title}: {state}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clear the methodist memo keys of a subtree so the next "
            "'Generate description' run regenerates it. Preview by default. "
            "The next run overwrites each node's Final description from the "
            "new Raw and resets its approval (the previous Final is kept as a "
            "snapshot)."
        )
    )
    parser.add_argument("node_id", type=uuid.UUID, help="CourseNode id (required)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Clear the keys. Without it nothing is changed.",
    )
    args = parser.parse_args(argv)

    with get_sync_session() as session:
        try:
            plan = plan_reset(session, args.node_id)
        except ResetError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        for line in describe(plan):
            print(line)
        if not args.apply:
            print("Preview only: nothing changed. Re-run with --apply to reset.")
            return 0
        changed = apply_reset(session, plan)
    print(
        f"Reset {changed} Raw rows. Run 'Generate description' on node "
        f"{args.node_id} to regenerate the subtree; its Final descriptions will "
        "need approval again."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
