"""Learning order of a course: when a student meets each material (task 10).

One answer to "which comes first?" for every reader that lists course
materials in the order they are studied — "left to right, bottom up":

* nested nodes are studied in sibling order: ``CourseNode.order`` ASC with
  unnumbered nodes after numbered ones, then ``created_at`` (the order the
  tree itself stores, ``orm.CourseNode.order``);
* a node's own materials open only after everything nested in it — the tree
  is walked children first, then the node — so the root's materials come last;
* inside a node, documents follow ``AuthoredDocument.order`` and segments
  ``DocumentSegment.order``.

A remaining tie is broken the way the student's portal tree breaks it — a
document by ``created_at`` (``portal_courses._project_node``) — and then by id,
so equal keys never leave the order to chance and one document's segments
never interleave with another's.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class TreeNode:
    """The fields of a ``CourseNode`` the learning order reads."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    order: int | None
    created_at: datetime


class MaterialPlace(NamedTuple):
    """Where a segment sits in the course: its node, document and own order."""

    course_node_id: uuid.UUID
    document_order: int
    document_created_at: datetime
    document_id: uuid.UUID
    segment_order: int


def in_learning_order(
    items: Iterable[T],
    tree: Iterable[TreeNode],
    *,
    place: Callable[[T], MaterialPlace],
) -> list[T]:
    """``items`` sorted in the order a student meets them in the course.

    ``tree`` is the whole course — every node a place can name. A place whose
    node is not in it means a broken tree, and raises rather than guessing a
    position for it.
    """
    rank = _node_ranks(tree)

    def key(item: T) -> tuple[int, int, datetime, uuid.UUID, int]:
        node_id, document_order, created_at, document_id, segment_order = place(item)
        try:
            node_rank = rank[node_id]
        except KeyError:
            msg = f"course node {node_id} is not in the course tree"
            raise ValueError(msg) from None
        return node_rank, document_order, created_at, document_id, segment_order

    return sorted(items, key=key)


def _node_ranks(tree: Iterable[TreeNode]) -> dict[uuid.UUID, int]:
    """Each node's position in a children-first walk of the tree."""
    nodes = list(tree)
    ids = {node.id for node in nodes}
    children: defaultdict[uuid.UUID | None, list[TreeNode]] = defaultdict(list)
    for node in nodes:
        # A node whose parent is outside the tree is a root of the walk.
        children[node.parent_id if node.parent_id in ids else None].append(node)

    ranks: dict[uuid.UUID, int] = {}

    def walk(parent_id: uuid.UUID | None) -> None:
        for node in sorted(children[parent_id], key=_sibling_key):
            walk(node.id)
            ranks[node.id] = len(ranks)

    walk(None)
    return ranks


def _sibling_key(node: TreeNode) -> tuple[bool, int, datetime, uuid.UUID]:
    """``order`` ASC NULLS LAST, then ``created_at``, then id."""
    return node.order is None, node.order or 0, node.created_at, node.id
