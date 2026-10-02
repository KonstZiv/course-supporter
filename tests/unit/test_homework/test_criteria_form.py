"""Unit tests for the criterion form (mentor-rebuild task 08, K2).

Pure functions and models, no session and no model: the weight numbers, the
identifiers code assigns, the filter that keeps only the input's concepts,
the form's own consistency rules, and the stored document.
"""

from __future__ import annotations

import doctest
from typing import Any

import pytest
from pydantic import ValidationError

from course_supporter.criteria_kinds import WeightCategory
from course_supporter.homework import criteria_form
from course_supporter.homework.criteria_form import (
    MAX_CONCEPTS,
    MAX_CRITERIA,
    MAX_POINT_CHARS,
    MAX_POINTS,
    MAX_TEXT_CHARS,
    WEIGHT_NUMBERS,
    CheckMethod,
    Criterion,
    CriterionDraft,
    check_methods_for,
    compose_criteria,
    criteria_from_document,
    criteria_to_document,
    keep_input_concepts,
)
from course_supporter.models.source import AssignmentType

_INPUT_CONCEPTS = ["Recursion", "Base Case", "HTML Template"]


def _draft(**overrides: Any) -> CriterionDraft:
    fields: dict[str, Any] = {
        "text": "Has a base case",
        "evidence": "an explicit if-return for the smallest input",
        "weight": "must",
        "check_method": "model_verdict",
    }
    fields.update(overrides)
    return CriterionDraft.model_validate(fields)


def _stored(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "id": "c1",
        "text": "Has a base case",
        "evidence": "an explicit if-return for the smallest input",
        "weight": "must",
        "check_method": "model_verdict",
        "soft_descent": False,
        "concepts": [],
        "mandatory_points": [],
    }
    fields.update(overrides)
    return fields


class TestWeightNumbers:
    def test_each_category_counts_for_its_number(self) -> None:
        assert dict(WEIGHT_NUMBERS) == {
            WeightCategory.MUST: 3,
            WeightCategory.SHOULD: 2,
            WeightCategory.MAY: 1,
        }

    @pytest.mark.parametrize(
        ("weight", "number"), [("must", 3), ("should", 2), ("may", 1)]
    )
    def test_a_criterion_takes_the_number_of_its_category(
        self, weight: str, number: int
    ) -> None:
        criterion = Criterion.model_validate(_stored(weight=weight))
        assert criterion.weight_number == number

    def test_the_number_is_computed_never_stored(self) -> None:
        document = criteria_to_document([Criterion.model_validate(_stored())])
        assert "weight_number" not in document[0]
        assert document[0]["weight"] == "must"


class TestIdentifiers:
    def test_criteria_are_numbered_in_order_by_code(self) -> None:
        drafts = [_draft(text=f"requirement {n}") for n in range(1, 4)]
        composition = compose_criteria(drafts, [], input_concepts=[])
        assert [c.id for c in composition.criteria] == ["c1", "c2", "c3"]
        assert [c.text for c in composition.criteria] == [
            "requirement 1",
            "requirement 2",
            "requirement 3",
        ]

    def test_points_are_numbered_within_their_own_criterion(self) -> None:
        drafts = [
            _draft(check_method="mandatory_points", mandatory_points=["a", "b"]),
            _draft(),
            _draft(check_method="mandatory_points", mandatory_points=["c", "d", "e"]),
        ]
        composition = compose_criteria(drafts, [], input_concepts=[])
        points = [[p.id for p in c.mandatory_points] for c in composition.criteria]
        assert points == [["c1.p1", "c1.p2"], [], ["c3.p1", "c3.p2", "c3.p3"]]
        texts = [[p.text for p in c.mandatory_points] for c in composition.criteria]
        assert texts == [["a", "b"], [], ["c", "d", "e"]]

    def test_the_same_drafts_get_the_same_identifiers(self) -> None:
        drafts = [
            _draft(check_method="mandatory_points", mandatory_points=["a", "b"]),
            _draft(text="another"),
        ]
        first = compose_criteria(drafts, [], input_concepts=[])
        second = compose_criteria(drafts, [], input_concepts=[])
        assert first == second
        ids = [c.id for c in first.criteria]
        assert len(set(ids)) == len(ids)

    def test_a_draft_cannot_bring_its_own_identifier(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            _draft(id="c9")

    def test_a_draft_cannot_bring_a_point_identifier(self) -> None:
        # A draft's points are bare texts: a point with an id is not one.
        with pytest.raises(ValidationError, match="valid string"):
            _draft(
                check_method="mandatory_points",
                mandatory_points=[{"id": "c1.p1", "text": "a"}],
            )


class TestConcepts:
    def test_an_invented_concept_is_dropped_and_counted(self) -> None:
        drafts = [
            _draft(concepts=["Recursion", "Memoization"]),
            _draft(concepts=["Tail Calls", "Base Case", "Monads"]),
        ]
        composition = compose_criteria(drafts, [], input_concepts=_INPUT_CONCEPTS)
        assert [c.concepts for c in composition.criteria] == [
            ("Recursion",),
            ("Base Case",),
        ]
        assert composition.dropped_concept_count == 3

    def test_a_spelling_variant_is_kept_in_the_inputs_spelling(self) -> None:
        drafts = [_draft(concepts=["recursion", "base-cases", "html templates"])]
        composition = compose_criteria(drafts, [], input_concepts=_INPUT_CONCEPTS)
        assert composition.criteria[0].concepts == (
            "Recursion",
            "Base Case",
            "HTML Template",
        )
        assert composition.dropped_concept_count == 0

    def test_a_concept_named_twice_is_kept_or_counted_once(self) -> None:
        kept, dropped = keep_input_concepts(
            ["Recursion", "recursion", "Monads", "monads"], _INPUT_CONCEPTS
        )
        assert kept == ("Recursion",)
        assert dropped == 1

    def test_without_concepts_in_the_input_every_named_one_is_dropped(self) -> None:
        drafts = [_draft(concepts=["Recursion"]), _draft(concepts=["Base Case"])]
        composition = compose_criteria(drafts, [], input_concepts=[])
        assert all(c.concepts == () for c in composition.criteria)
        assert composition.dropped_concept_count == 2

    def test_the_first_spelling_of_a_key_in_the_input_wins(self) -> None:
        kept, _ = keep_input_concepts(
            ["html template"], ["HTML Templates", "Html template"]
        )
        assert kept == ("HTML Templates",)


class TestSoftDescent:
    def test_only_a_code_test_criterion_is_marked(self) -> None:
        drafts = [
            _draft(check_method="code_test"),
            _draft(),
            _draft(check_method="mandatory_points", mandatory_points=["a"]),
        ]
        composition = compose_criteria(drafts, [], input_concepts=[])
        assert [c.soft_descent for c in composition.criteria] == [True, False, False]

    @pytest.mark.parametrize(
        ("method", "mark"),
        [("code_test", False), ("model_verdict", True), ("mandatory_points", True)],
    )
    def test_the_mark_and_the_method_go_together(self, method: str, mark: bool) -> None:
        points = [{"id": "c1.p1", "text": "a"}] if method == "mandatory_points" else []
        with pytest.raises(ValidationError, match="soft_descent"):
            Criterion.model_validate(
                _stored(check_method=method, soft_descent=mark, mandatory_points=points)
            )


class TestCheckMethods:
    def test_only_a_project_admits_code_test(self) -> None:
        for task_type in AssignmentType:
            admitted = check_methods_for(task_type.value)
            if task_type is AssignmentType.PROJECT:
                assert admitted == frozenset(CheckMethod)
            else:
                assert admitted == {
                    CheckMethod.MODEL_VERDICT,
                    CheckMethod.MANDATORY_POINTS,
                }


class TestPointsFollowTheMethod:
    def test_mandatory_points_method_needs_points(self) -> None:
        with pytest.raises(ValidationError, match="needs a non-empty"):
            _draft(check_method="mandatory_points")

    @pytest.mark.parametrize("method", ["model_verdict", "code_test"])
    def test_points_are_refused_for_another_method(self, method: str) -> None:
        with pytest.raises(ValidationError, match="must be empty unless"):
            _draft(check_method=method, mandatory_points=["a"])

    def test_the_stored_form_holds_the_same_rule(self) -> None:
        with pytest.raises(ValidationError, match="needs a non-empty"):
            Criterion.model_validate(_stored(check_method="mandatory_points"))
        with pytest.raises(ValidationError, match="must be empty unless"):
            Criterion.model_validate(
                _stored(mandatory_points=[{"id": "c1.p1", "text": "a"}])
            )


class TestLimits:
    """Decision 17 of ``TASK.md`` section 9, on both the draft and the stored form."""

    def test_text_and_evidence_up_to_the_limit(self) -> None:
        _draft(text="x" * MAX_TEXT_CHARS, evidence="y" * MAX_TEXT_CHARS)
        with pytest.raises(ValidationError, match="at most 600"):
            _draft(text="x" * (MAX_TEXT_CHARS + 1))
        with pytest.raises(ValidationError, match="at most 600"):
            _draft(evidence="y" * (MAX_TEXT_CHARS + 1))

    def test_points_up_to_the_limit_in_number_and_length(self) -> None:
        _draft(check_method="mandatory_points", mandatory_points=["p"] * MAX_POINTS)
        with pytest.raises(ValidationError, match="at most 10"):
            _draft(
                check_method="mandatory_points",
                mandatory_points=["p"] * (MAX_POINTS + 1),
            )
        with pytest.raises(ValidationError, match="at most 300"):
            _draft(
                check_method="mandatory_points",
                mandatory_points=["p" * (MAX_POINT_CHARS + 1)],
            )

    def test_concepts_up_to_the_limit(self) -> None:
        _draft(concepts=["c"] * MAX_CONCEPTS)
        with pytest.raises(ValidationError, match="at most 10"):
            _draft(concepts=["c"] * (MAX_CONCEPTS + 1))

    def test_the_stored_form_holds_the_same_limits(self) -> None:
        with pytest.raises(ValidationError, match="at most 600"):
            Criterion.model_validate(_stored(text="x" * (MAX_TEXT_CHARS + 1)))
        with pytest.raises(ValidationError, match="at most 10"):
            Criterion.model_validate(_stored(concepts=["c"] * (MAX_CONCEPTS + 1)))

    @pytest.mark.parametrize("field", ["text", "evidence"])
    def test_a_blank_field_is_refused(self, field: str) -> None:
        with pytest.raises(ValidationError, match="at least 1 character"):
            _draft(**{field: "   "})


class TestStoredForm:
    def test_a_stored_criterion_refuses_an_extra_field(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Criterion.model_validate(_stored(weight_number=3))

    @pytest.mark.parametrize("identifier", ["c0", "C1", "1", "c1.p1", "c01", ""])
    def test_a_criterion_id_has_the_code_form(self, identifier: str) -> None:
        with pytest.raises(ValidationError, match="pattern"):
            Criterion.model_validate(_stored(id=identifier))

    @pytest.mark.parametrize("identifier", ["c1.p0", "c1.p01", "c1.p", "c1.p1 "])
    def test_a_point_id_has_the_code_form(self, identifier: str) -> None:
        with pytest.raises(ValidationError, match="pattern"):
            Criterion.model_validate(
                _stored(
                    check_method="mandatory_points",
                    mandatory_points=[{"id": identifier, "text": "a"}],
                )
            )

    def test_a_point_id_extends_its_criterion_id(self) -> None:
        with pytest.raises(ValidationError, match=r"must start with 'c1\.p'"):
            Criterion.model_validate(
                _stored(
                    check_method="mandatory_points",
                    mandatory_points=[{"id": "c12.p1", "text": "a"}],
                )
            )

    def test_point_ids_do_not_repeat_within_a_criterion(self) -> None:
        with pytest.raises(ValidationError, match="repeat"):
            Criterion.model_validate(
                _stored(
                    check_method="mandatory_points",
                    mandatory_points=[
                        {"id": "c1.p1", "text": "a"},
                        {"id": "c1.p1", "text": "b"},
                    ],
                )
            )


class TestDocument:
    def test_a_composition_round_trips_through_its_document(self) -> None:
        drafts = [
            _draft(concepts=["Recursion"]),
            _draft(check_method="mandatory_points", mandatory_points=["a", "b"]),
            _draft(check_method="code_test", weight="may"),
        ]
        composition = compose_criteria(drafts, [], input_concepts=_INPUT_CONCEPTS)
        document = criteria_to_document(composition.criteria)
        assert document[1]["mandatory_points"] == [
            {"id": "c2.p1", "text": "a"},
            {"id": "c2.p2", "text": "b"},
        ]
        assert criteria_from_document(document) == composition.criteria

    def test_criterion_ids_do_not_repeat_in_a_document(self) -> None:
        with pytest.raises(ValidationError, match="criterion ids repeat"):
            criteria_from_document([_stored(), _stored(text="another")])

    def test_a_document_is_never_empty(self) -> None:
        with pytest.raises(ValidationError, match="at least 1 item"):
            criteria_from_document([])

    def test_a_document_holds_at_most_the_limit(self) -> None:
        many = [_stored(id=f"c{n}") for n in range(1, MAX_CRITERIA + 2)]
        criteria_from_document(many[:MAX_CRITERIA])
        with pytest.raises(ValidationError, match="at most 60"):
            criteria_from_document(many)

    def test_writing_a_document_checks_the_whole_list(self) -> None:
        one = Criterion.model_validate(_stored())
        with pytest.raises(ValidationError, match="criterion ids repeat"):
            criteria_to_document([one, one])

    def test_contradictions_are_carried_as_given(self) -> None:
        composition = compose_criteria(
            [_draft()], ["the node says loops are not covered"], input_concepts=[]
        )
        assert composition.contradictions == ("the node says loops are not covered",)


def test_the_module_examples_are_executed() -> None:
    """Run the docstring examples explicitly — the gate does not (``DD-SP-BC``).

    The shape of task 04: assert both that they passed AND that there were
    some, because ``failed == 0`` is also true of a module with no examples.
    """
    results = doctest.testmod(criteria_form, verbose=False)
    assert results.attempted > 0, "the module's documentation lost its examples"
    assert results.failed == 0
