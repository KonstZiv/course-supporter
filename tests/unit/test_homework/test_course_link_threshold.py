"""The score threshold of the course links (task 10, decision of 2026-11-13).

Production 2026-11-12: a criterion about the archive's structure got two places
of 0.14 — one concept of five — and the model gave both, among them the
material on the course's final project for the homework of lecture 1. The
owner's decision: better "no fitting place" than a wrong link; a place below
:data:`MIN_COURSE_LINK_SCORE` is not shown to the explanation model.

The pure half, without a database (the walk through a live course is in
``tests/integration/test_course_link_delivery_db.py``,
``test_review_explanation_stage_db.py`` and ``test_text_review_e2e_db.py``):

* lock a — the row keeps every place; only :meth:`FoundCourseLinks.shown`
  cuts, at the threshold: 0.30 is shown, 0.29 is not, 0.14 is not;
* lock b — a criterion left with no place is shown an empty list;
* the ranks of the places that stay do not move (lock ґ's other half is the
  digest in ``tests/unit/test_prompts/test_course_link_input_unchanged.py``).
"""

from __future__ import annotations

import uuid

import pytest

from course_supporter.homework import criteria_verdicts
from course_supporter.homework.course_links import (
    MIN_COURSE_LINK_SCORE,
    CriterionLinks,
    FoundCourseLinks,
    LinkCandidate,
    TreeRelation,
    link_confidence,
)
from course_supporter.homework.review_explanation import links_to_show
from course_supporter.storage.document_segment_repository import SegmentAnchor


def _place(confidence: float, *, title: str = "Матеріал") -> LinkCandidate:
    return LinkCandidate(
        segment_id=uuid.uuid4(),
        authored_document_id=uuid.uuid4(),
        material_title=title,
        source_type="text",
        anchor=SegmentAnchor("paragraph", 3, 4),
        excerpt="Початок матеріалу.",
        later_in_course=False,
        confidence=confidence,
        matched_concepts=("archive",),
    )


def _found(**kept: list[LinkCandidate]) -> FoundCourseLinks:
    return FoundCourseLinks(
        criteria=tuple(
            CriterionLinks(item_id=item_id, candidates=tuple(places))
            for item_id, places in kept.items()
        )
    )


def test_the_threshold_is_the_owners_decision() -> None:
    assert MIN_COURSE_LINK_SCORE == 0.3


class TestWhatTheModelIsShown:
    """Lock a: the row keeps every place, ``shown`` cuts at the threshold."""

    @pytest.mark.parametrize(
        ("confidence", "is_shown"),
        [
            pytest.param(0.14, False, id="production-0.14"),
            pytest.param(0.29, False, id="0.29"),
            pytest.param(0.30, True, id="0.30"),
            pytest.param(0.31, True, id="0.31"),
        ],
    )
    def test_at_the_threshold_is_shown_below_it_is_not(
        self, confidence: float, is_shown: bool
    ) -> None:
        place = _place(confidence)
        found = _found(c1=[place])

        assert found.shown("c1") == ((("c1.l1", place),) if is_shown else ())

    def test_the_row_keeps_every_place_and_shown_cuts(self) -> None:
        """Nothing is dropped when the row is written or read back."""
        strong, weak, weaker = _place(0.6), _place(0.14), _place(0.14)
        found = _found(c1=[strong, weak, weaker])

        kept = FoundCourseLinks.model_validate(found.model_dump(mode="json"))

        assert kept.candidates_of("c1") == (strong, weak, weaker)
        assert kept.shown("c1") == (("c1.l1", strong),)

    def test_a_sum_that_is_exactly_the_threshold_in_arithmetic_is_shown(self) -> None:
        """3/8 of the concepts, far, 3rd of 8 in order: 0.7·3/8 + 0.1·3/8 = 0.3,
        which the float sum puts a hair below. The place is on the line, and
        the line is inclusive."""
        confidence = link_confidence(
            coverage=3 / 8, relation=TreeRelation.FAR, order_closeness=3 / 8
        )
        assert confidence < MIN_COURSE_LINK_SCORE  # the premise of the rounding
        place = _place(confidence)

        assert _found(c1=[place]).shown("c1") == (("c1.l1", place),)

    def test_the_places_that_stay_keep_their_ranks(self) -> None:
        """The cut takes the tail of a best-first list: c3.l1 and c3.l2 are the
        ids they always were, and a made-up c3.l3 resolves to nothing."""
        first, second, weak = _place(0.9), _place(0.5), _place(0.14)
        found = _found(c3=[first, second, weak])

        assert found.shown("c3") == (("c3.l1", first), ("c3.l2", second))
        assert dict(found.shown("c3")).get("c3.l3") is None


class TestACriterionWithOnlyWeakPlaces:
    """Lock b: shown an empty list, as a criterion the course does not teach."""

    def test_every_place_below_the_threshold_leaves_none_to_show(self) -> None:
        found = _found(c1=[_place(0.14), _place(0.14)])

        assert found.shown("c1") == ()
        assert len(found.candidates_of("c1")) == 2

    def test_the_model_is_shown_an_empty_list_for_it_and_places_for_the_rest(
        self,
    ) -> None:
        strong = _place(0.6, title="Рекурсія")
        found = _found(c1=[_place(0.14), _place(0.14)], c2=[strong])

        shown = links_to_show(["c1", "c2", "c3"], found)

        assert shown["c1"] == ()
        assert [link.id for link in shown["c2"]] == ["c2.l1"]
        assert [link.material for link in shown["c2"]] == ["Рекурсія"]
        assert shown["c3"] == ()


def test_the_verdict_rules_were_raised_for_the_threshold() -> None:
    """The threshold changes what a review says: the version moved 4 → 5, so a
    review written before it is never given again as a copy."""
    assert criteria_verdicts.VERDICT_RULES_VERSION >= 5
