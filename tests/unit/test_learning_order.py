"""Unit tests for the course learning order (task 10, decision A).

The pure half of the order: the tree walk and the in-node keys, without a
database. The end-to-end lock through ``search_by_concepts`` lives in
``tests/integration/test_document_segment_repository.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from course_supporter.learning_order import MaterialPlace, TreeNode, in_learning_order

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _node(parent: TreeNode | None, order: int | None, *, minutes: int = 0) -> TreeNode:
    return TreeNode(
        id=uuid.uuid4(),
        parent_id=parent.id if parent else None,
        order=order,
        created_at=_T0 + timedelta(minutes=minutes),
    )


def _place(
    node: TreeNode,
    *,
    document_order: int = 0,
    document_id: uuid.UUID | None = None,
    segment_order: int = 0,
) -> MaterialPlace:
    return MaterialPlace(
        course_node_id=node.id,
        document_order=document_order,
        document_created_at=_T0,
        document_id=document_id or uuid.uuid4(),
        segment_order=segment_order,
    )


def _sorted(places: list[MaterialPlace], tree: list[TreeNode]) -> list[MaterialPlace]:
    return in_learning_order(places, tree, place=lambda p: p)


def test_children_before_their_node_root_last() -> None:
    """Nested nodes first in sibling order, then the node itself; root last."""
    root = _node(None, 0)
    block_2 = _node(root, 2)
    block_1 = _node(root, 1)
    lesson_1b = _node(block_1, 2)
    lesson_1a = _node(block_1, 1)
    lesson_2a = _node(block_2, 1)
    tree = [root, block_2, block_1, lesson_1b, lesson_1a, lesson_2a]
    expected = [
        _place(lesson_1a),
        _place(lesson_1b),
        _place(block_1),
        _place(lesson_2a),
        _place(block_2),
        _place(root),
    ]

    assert _sorted(list(reversed(expected)), tree) == expected


def test_unnumbered_sibling_after_numbered() -> None:
    """A node without ``order`` follows its numbered siblings, even if older."""
    root = _node(None, 0)
    unnumbered = _node(root, None, minutes=0)
    numbered = _node(root, 7, minutes=5)
    expected = [_place(numbered), _place(unnumbered), _place(root)]

    assert _sorted(list(reversed(expected)), [root, unnumbered, numbered]) == expected


def test_created_at_breaks_equal_sibling_order() -> None:
    """Siblings with the same ``order`` follow ``created_at``."""
    root = _node(None, 0)
    later = _node(root, 1, minutes=9)
    earlier = _node(root, 1, minutes=1)
    expected = [_place(earlier), _place(later), _place(root)]

    assert _sorted(list(reversed(expected)), [root, later, earlier]) == expected


def test_in_node_by_document_then_segment() -> None:
    """Inside a node: document order, then segment order; documents never mix."""
    root = _node(None, 0)
    doc_a, doc_b = sorted([uuid.uuid4(), uuid.uuid4()])
    expected = [
        _place(root, document_order=0, document_id=doc_a, segment_order=0),
        _place(root, document_order=0, document_id=doc_a, segment_order=1),
        _place(root, document_order=0, document_id=doc_b, segment_order=0),
        _place(root, document_order=0, document_id=doc_b, segment_order=1),
        _place(root, document_order=1, segment_order=0),
    ]
    shuffled = [expected[i] for i in (4, 3, 1, 2, 0)]

    assert _sorted(shuffled, [root]) == expected


def test_node_outside_tree_raises() -> None:
    """A place naming a node the tree does not have is a broken tree."""
    root = _node(None, 0)
    stray = _node(None, 0)

    with pytest.raises(ValueError, match="not in the course tree"):
        _sorted([_place(stray)], [root])
