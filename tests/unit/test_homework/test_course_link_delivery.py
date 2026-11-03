"""The pure parts of the check before delivery (task 10, K6).

The rule of a replacement (:func:`replacement_for`), a settled place as the
review's reference (:func:`reference_of`), the identifiers a place is shown
and resolved by (:meth:`FoundCourseLinks.shown`), and the settled links read
back (:meth:`FoundCourseLinks.settled_of`). The check against a live course —
what is gone, the new search, the trace, the snapshot — is pinned in
``tests/integration/test_course_link_delivery_db.py``.
"""

from __future__ import annotations

import doctest
import uuid

from course_supporter.homework import course_link_delivery
from course_supporter.homework.course_link_delivery import (
    reference_of,
    replacement_for,
)
from course_supporter.homework.course_links import (
    CriterionLinks,
    FoundCourseLinks,
    LinkCandidate,
)
from course_supporter.models.review_structure import Position, Reference
from course_supporter.storage.document_segment_repository import SegmentAnchor

_A, _B = uuid.uuid4(), uuid.uuid4()


def _place(
    material: uuid.UUID, confidence: float, *, later: bool = False
) -> LinkCandidate:
    return LinkCandidate(
        segment_id=uuid.uuid4(),
        authored_document_id=material,
        material_title="Рекурсія",
        source_type="audio",
        anchor=SegmentAnchor("time", 725.4, 800.0),
        excerpt="Рекурсія — це коли функція викликає сама себе.",
        later_in_course=later,
        confidence=confidence,
        matched_concepts=("recursion",),
    )


class TestAReplacement:
    def test_one_of_the_same_material_before_a_more_confident_one(self) -> None:
        gone, other, same = _place(_A, 0.9), _place(_B, 0.95), _place(_A, 0.4)

        assert replacement_for(gone, [other, same]) is same

    def test_the_most_confident_of_the_same_material(self) -> None:
        gone = _place(_A, 0.9)
        weaker, stronger = _place(_A, 0.4), _place(_A, 0.6)

        assert replacement_for(gone, [weaker, stronger]) is stronger

    def test_without_one_of_the_same_material_the_most_confident(self) -> None:
        gone, weaker, stronger = _place(_A, 0.9), _place(_B, 0.5), _place(_B, 0.8)

        assert replacement_for(gone, [weaker, stronger]) is stronger

    def test_equal_confidences_keep_the_finders_order(self) -> None:
        gone, first, second = _place(_A, 0.9), _place(_B, 0.5), _place(_B, 0.5)

        assert replacement_for(gone, [first, second]) is first

    def test_a_place_already_given_is_never_given_again(self) -> None:
        gone, same, other = _place(_A, 0.9), _place(_A, 0.9), _place(_B, 0.1)

        assert replacement_for(gone, [same, other], taken=[same.segment_id]) is other

    def test_nothing_found_is_none(self) -> None:
        gone, same = _place(_A, 0.9), _place(_A, 0.9)

        assert replacement_for(gone, []) is None
        assert replacement_for(gone, [same], taken=[same.segment_id]) is None


class TestTheReference:
    def test_a_settled_place_as_the_review_reads_it(self) -> None:
        place = _place(_A, 0.9, later=True)

        assert reference_of(place) == Reference(
            title="Рекурсія",
            url=None,
            position=Position(kind="audio", value="12:05"),
            excerpt="Рекурсія — це коли функція викликає сама себе.",
            segment_id=str(place.segment_id),
            later_in_course=True,
        )


class TestTheIdentifiers:
    def test_shown_by_criterion_and_rank_three_at_most(self) -> None:
        places = tuple(_place(_A, 0.9 - n / 10) for n in range(4))
        found = FoundCourseLinks(
            criteria=(CriterionLinks(item_id="c3", candidates=places),)
        )

        assert found.shown("c3") == (
            ("c3.l1", places[0]),
            ("c3.l2", places[1]),
            ("c3.l3", places[2]),
        )
        assert found.shown("c5") == ()

    def test_settled_reads_back_through_storage(self) -> None:
        place = _place(_A, 0.9)
        found = FoundCourseLinks(
            criteria=(CriterionLinks(item_id="c3", candidates=(place,)),),
            settled=(
                CriterionLinks(item_id="c3", candidates=(place,)),
                CriterionLinks(item_id="c5", candidates=()),
            ),
        )

        kept = FoundCourseLinks.model_validate(found.model_dump(mode="json"))

        assert kept == found
        assert kept.settled_of("c3") == (place,)
        assert kept.settled_of("c5") == ()

    def test_a_row_kept_before_k6_is_not_settled(self) -> None:
        """The search's row of K5 has no ``settled``: it reads as not yet."""
        kept = FoundCourseLinks.model_validate({"criteria": []})

        assert kept.settled is None
        assert kept.settled_of("c3") == ()


class TestDocstrings:
    def test_the_examples_in_the_module_run(self) -> None:
        results = doctest.testmod(course_link_delivery, raise_on_error=False)

        assert results.attempted > 0
        assert results.failed == 0
