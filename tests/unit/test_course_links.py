"""Unit tests for the pure half of the course-link finder (task 10, commit K3).

The confidence, the tree relation and the excerpt, without a database. The
port end to end — matching, earlier/later, selection, the candidate filters —
is locked in ``tests/integration/test_course_links_db.py``.
"""

from __future__ import annotations

import uuid

import pytest

from course_supporter.homework.course_links import (
    COVERAGE_WEIGHT,
    EXCERPT_CHARS,
    ORDER_WEIGHT,
    TREE_WEIGHT,
    TreeRelation,
    excerpt_of,
    link_confidence,
    tree_relation,
)

_RELATIONS = list(TreeRelation)


class TestLinkConfidence:
    def test_weights_sum_to_one_and_bound_the_measure(self) -> None:
        """The measure spans exactly 0..1."""
        total = COVERAGE_WEIGHT + TREE_WEIGHT + ORDER_WEIGHT
        assert total == pytest.approx(1.0)
        lowest = link_confidence(
            coverage=0.0, relation=TreeRelation.FAR, order_closeness=0.0
        )
        highest = link_confidence(
            coverage=1.0, relation=TreeRelation.SAME_NODE, order_closeness=1.0
        )
        assert (lowest, highest) == (0.0, pytest.approx(1.0))

    @pytest.mark.parametrize("relation", _RELATIONS)
    @pytest.mark.parametrize("closeness", [0.0, 1.0])
    def test_coverage_outweighs_the_other_two(
        self, relation: TreeRelation, closeness: float
    ) -> None:
        """Every concept of the criterion beats half of them, however placed."""
        full = link_confidence(
            coverage=1.0, relation=TreeRelation.FAR, order_closeness=0.0
        )
        half = link_confidence(
            coverage=0.5, relation=relation, order_closeness=closeness
        )
        assert full > half

    def test_tree_relation_ranks_same_near_far(self) -> None:
        """At equal coverage and closeness: same node > near > far."""
        same, near, far = (
            link_confidence(coverage=0.5, relation=r, order_closeness=0.5)
            for r in (TreeRelation.SAME_NODE, TreeRelation.NEAR, TreeRelation.FAR)
        )
        assert same > near > far

    def test_closer_in_learning_order_is_better(self) -> None:
        """At equal coverage and relation: closer to the task ranks higher."""
        closer = link_confidence(
            coverage=0.5, relation=TreeRelation.FAR, order_closeness=0.9
        )
        further = link_confidence(
            coverage=0.5, relation=TreeRelation.FAR, order_closeness=0.1
        )
        assert closer > further


# root → (block 1 → lesson 1a), (block 2 → lesson 2a, lesson 2b → topic)
_ROOT, _BLOCK_1, _BLOCK_2, _LESSON_1A, _LESSON_2A, _LESSON_2B, _TOPIC = (
    uuid.uuid4() for _ in range(7)
)
_PARENTS: dict[uuid.UUID, uuid.UUID | None] = {
    _ROOT: None,
    _BLOCK_1: _ROOT,
    _BLOCK_2: _ROOT,
    _LESSON_1A: _BLOCK_1,
    _LESSON_2A: _BLOCK_2,
    _LESSON_2B: _BLOCK_2,
    _TOPIC: _LESSON_2B,
}


class TestTreeRelation:
    """Relations to a task on lesson 2b, in the tree of ``_PARENTS``."""

    @pytest.mark.parametrize(
        ("node", "expected"),
        [
            pytest.param(_LESSON_2B, TreeRelation.SAME_NODE, id="same"),
            pytest.param(_LESSON_2A, TreeRelation.NEAR, id="sibling"),
            pytest.param(_BLOCK_2, TreeRelation.NEAR, id="parent"),
            pytest.param(_ROOT, TreeRelation.NEAR, id="ancestor"),
            pytest.param(_TOPIC, TreeRelation.NEAR, id="descendant"),
            pytest.param(_BLOCK_1, TreeRelation.FAR, id="parent-sibling"),
            pytest.param(_LESSON_1A, TreeRelation.FAR, id="cousin"),
        ],
    )
    def test_relation_to_the_task_node(
        self, node: uuid.UUID, expected: TreeRelation
    ) -> None:
        assert tree_relation(node, _LESSON_2B, _PARENTS) is expected

    def test_children_of_the_root_are_siblings(self) -> None:
        """A shared parent counts even when it is the root."""
        assert tree_relation(_BLOCK_1, _BLOCK_2, _PARENTS) is TreeRelation.NEAR

    def test_a_sibling_child_is_far(self) -> None:
        """The task's sibling is near, the sibling's child is not."""
        assert tree_relation(_TOPIC, _LESSON_2A, _PARENTS) is TreeRelation.FAR


class TestExcerpt:
    def test_cyrillic_cut_by_characters(self) -> None:
        """Longer than the limit: exactly the limit, whole letters, valid UTF-8."""
        content = "Цикл «для кожного» проходить по всіх елементах списку. " * 10
        assert len(content) > EXCERPT_CHARS

        excerpt = excerpt_of(content)

        assert excerpt is not None
        assert len(excerpt) == EXCERPT_CHARS
        assert content.startswith(excerpt)
        assert excerpt.encode("utf-8").decode("utf-8") == excerpt

    def test_short_text_whole(self) -> None:
        assert excerpt_of("Рекурсія") == "Рекурсія"

    @pytest.mark.parametrize("content", ["", "  \n\t "])
    def test_empty_text_none(self, content: str) -> None:
        assert excerpt_of(content) is None
