"""Unit tests for the author's edit of a criteria list (mentor-rebuild task 08, K5).

The pure parts: :func:`apply_edit` — identifiers kept or assigned, check
methods and concepts checked — the form an edit is sent in, and the README's
account of the routes. The service and the routes themselves run against a
live database in ``tests/integration/test_criteria_routes_db.py``.
"""

from __future__ import annotations

import doctest
import pathlib
import re
from typing import Any

import pytest
from pydantic import ValidationError

from course_supporter.api.app import app
from course_supporter.api.schemas import CriteriaOverrideRequest
from course_supporter.homework import criteria_edit_service
from course_supporter.homework.criteria_edit_service import (
    NOT_COMPOSED_MESSAGES,
    CriteriaReasonCode,
    CriteriaRefusalCode,
    CriteriaRefusedError,
    apply_edit,
)
from course_supporter.homework.criteria_form import (
    MAX_CONCEPTS,
    MAX_CRITERIA,
    MAX_POINT_CHARS,
    MAX_POINTS,
    MAX_TEXT_CHARS,
    Criterion,
    CriterionEdit,
    criteria_from_document,
)
from course_supporter.llm.error_categories import LadderStop

_CONCEPTS = ["Recursion", "Base Case", "HTML Template"]

_MODEL = criteria_from_document(
    [
        {
            "id": "c1",
            "text": "Handles n = 0",
            "evidence": "factorial(0) returns 1",
            "weight": "must",
            "check_method": "model_verdict",
            "soft_descent": False,
            "concepts": ["Base Case"],
            "mandatory_points": [],
        },
        {
            "id": "c2",
            "text": "Uses recursion",
            "evidence": "the function calls itself",
            "weight": "should",
            "check_method": "mandatory_points",
            "soft_descent": False,
            "concepts": ["Recursion"],
            "mandatory_points": [
                {"id": "c2.p1", "text": "a recursive call"},
                {"id": "c2.p2", "text": "a smaller argument each time"},
            ],
        },
        {
            "id": "c3",
            "text": "Readable names",
            "evidence": "names say what they hold",
            "weight": "may",
            "check_method": "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [],
        },
    ]
)


def _edit(**fields: Any) -> CriterionEdit:
    values: dict[str, Any] = {
        "text": "Explains the base case",
        "evidence": "a comment says why n = 0 stops",
        "weight": "should",
        "check_method": "model_verdict",
    }
    values.update(fields)
    return CriterionEdit.model_validate(values)


def _kept(criterion: Criterion, **changes: Any) -> CriterionEdit:
    """A criterion of the list, sent back as the author edits it."""
    values = criterion.model_dump(mode="json", exclude={"soft_descent"})
    values.update(changes)
    return CriterionEdit.model_validate(values)


def _apply(
    edits: list[CriterionEdit],
    *,
    base: tuple[Criterion, ...] = _MODEL,
    also_taken: tuple[Criterion, ...] = (),
    task_type: str = "task",
) -> tuple[Criterion, ...]:
    return apply_edit(
        edits,
        base=base,
        also_taken=also_taken,
        task_type=task_type,
        concepts=_CONCEPTS,
    )


def _refusal(edits: list[CriterionEdit], **kwargs: Any) -> CriteriaRefusedError:
    with pytest.raises(CriteriaRefusedError) as caught:
        _apply(edits, **kwargs)
    return caught.value


def test_the_module_examples_run() -> None:
    """The docstring examples are executed, not prose (``impl-rules`` habit)."""
    result = doctest.testmod(criteria_edit_service)

    assert result.attempted > 0, "the module has examples to run"
    assert result.failed == 0


class TestEveryReasonHasItsAdvice:
    """Task 09b, ``PRE-FLIGHT.md`` 9.2: a reason with nothing to do is unfinished.

    The module refuses to import without these (a guard at import), and these
    say so in the suite, where a change to the vocabulary shows by name.
    """

    def test_every_reason_code_has_a_message(self) -> None:
        assert set(NOT_COMPOSED_MESSAGES) == set(CriteriaReasonCode)
        assert all(message.strip() for message in NOT_COMPOSED_MESSAGES.values())

    def test_every_way_a_composition_ends_has_a_reason(self) -> None:
        reasons = criteria_edit_service._REASON_FOR_STOP

        assert set(reasons) == set(LadderStop)
        assert reasons[LadderStop.EXHAUSTED] is CriteriaReasonCode.MODELS_UNAVAILABLE
        assert reasons[LadderStop.OUTPUT_CEILING] is CriteriaReasonCode.LIMIT_REACHED
        assert reasons[LadderStop.MONEY_CEILING] is CriteriaReasonCode.LIMIT_REACHED


class TestIdentifiers:
    def test_what_is_edited_keeps_its_id_and_what_is_new_takes_the_next(
        self,
    ) -> None:
        result = _apply(
            [
                _kept(_MODEL[2], text="Names say what they hold"),
                _edit(),
                _kept(_MODEL[0]),
            ]
        )

        assert [criterion.id for criterion in result] == ["c3", "c4", "c1"]
        assert result[0].text == "Names say what they hold"

    def test_a_new_criterion_never_takes_an_id_another_list_gave(self) -> None:
        edit = _apply([_kept(_MODEL[0]), _kept(_MODEL[1])])

        result = _apply([_edit()], base=edit, also_taken=_MODEL)

        # The premise: without the model's list, ``c3`` would be free again.
        assert [c.id for c in _apply([_edit()], base=edit)] == ["c3"]
        assert [c.id for c in result] == ["c4"]

    def test_kept_points_keep_their_ids_and_new_ones_follow_their_criterions(
        self,
    ) -> None:
        points = [{"id": "c2.p2", "text": "a smaller argument"}, {"text": "no loop"}]

        [criterion] = _apply([_kept(_MODEL[1], mandatory_points=points)])

        assert [(p.id, p.text) for p in criterion.mandatory_points] == [
            ("c2.p2", "a smaller argument"),
            ("c2.p3", "no loop"),
        ]

    def test_a_new_criterions_points_are_counted_from_one(self) -> None:
        points = [{"text": "a base case"}, {"text": "a recursive call"}]

        [criterion] = _apply(
            [_edit(check_method="mandatory_points", mandatory_points=points)]
        )

        assert criterion.id == "c4"
        assert [p.id for p in criterion.mandatory_points] == ["c4.p1", "c4.p2"]

    def test_an_id_the_list_does_not_have_is_refused_not_reissued(self) -> None:
        refusal = _refusal(
            [
                _kept(_MODEL[0], id="c9"),
                _kept(
                    _MODEL[2],
                    check_method="mandatory_points",
                    mandatory_points=[{"id": "c3.p1", "text": "a point c3 never had"}],
                ),
                _edit(
                    check_method="mandatory_points",
                    mandatory_points=[{"id": "c2.p1", "text": "borrowed"}],
                ),
            ]
        )

        assert refusal.code is CriteriaRefusalCode.UNKNOWN_CRITERION_ID
        for identifier in ("c9", "c3.p1", "c2.p1"):
            assert identifier in refusal.details


class TestMethodsAndConcepts:
    def test_code_test_is_refused_outside_a_project(self) -> None:
        refusal = _refusal([_kept(_MODEL[0], check_method="code_test")])

        assert refusal.code is CriteriaRefusalCode.CHECK_METHOD_NOT_ADMITTED
        assert "criteria.0" in refusal.details

    def test_code_test_in_a_project_is_marked_soft_descent_by_the_code(self) -> None:
        [criterion] = _apply(
            [_kept(_MODEL[0], check_method="code_test")], task_type="project"
        )

        assert criterion.soft_descent is True

    def test_a_concept_outside_the_list_is_refused_not_dropped(self) -> None:
        refusal = _refusal([_kept(_MODEL[0], concepts=["Recursion", "Monads"])])

        assert refusal.code is CriteriaRefusalCode.UNKNOWN_CONCEPT
        assert "Monads" in refusal.details
        assert "Recursion" not in refusal.details

    def test_a_concept_is_kept_in_the_lists_spelling_once(self) -> None:
        [criterion] = _apply(
            [_kept(_MODEL[0], concepts=["html-templates", "recursion", "Recursion"])]
        )

        assert criterion.concepts == ("HTML Template", "Recursion")


class TestTheFormOfAnEdit:
    def test_soft_descent_is_not_the_authors_to_send(self) -> None:
        with pytest.raises(ValidationError, match="soft_descent"):
            CriterionEdit.model_validate(
                {**_edit().model_dump(mode="json"), "soft_descent": False}
            )

    @pytest.mark.parametrize(
        ("method", "points"),
        [("mandatory_points", []), ("model_verdict", [{"text": "a point"}])],
    )
    def test_points_come_with_their_method_and_only_with_it(
        self, method: str, points: list[dict[str, str]]
    ) -> None:
        with pytest.raises(ValidationError, match="mandatory_points"):
            _edit(check_method=method, mandatory_points=points)

    def test_a_point_id_is_sent_once(self) -> None:
        points = [{"id": "c2.p1", "text": "one"}, {"id": "c2.p1", "text": "two"}]

        with pytest.raises(ValidationError, match="repeat"):
            _kept(_MODEL[1], mandatory_points=points)

    def test_a_criterion_id_is_sent_once(self) -> None:
        with pytest.raises(ValidationError, match="repeat"):
            CriteriaOverrideRequest.model_validate(
                {
                    "criteria": [
                        _kept(_MODEL[0]).model_dump(mode="json"),
                        _kept(_MODEL[0]).model_dump(mode="json"),
                    ]
                }
            )

    @pytest.mark.parametrize(
        "criteria",
        [
            pytest.param([], id="none"),
            pytest.param([{}] * (MAX_CRITERIA + 1), id="too-many-criteria"),
            pytest.param([{"text": "x" * (MAX_TEXT_CHARS + 1)}], id="text"),
            pytest.param([{"evidence": "x" * (MAX_TEXT_CHARS + 1)}], id="evidence"),
            pytest.param(
                [
                    {
                        "check_method": "mandatory_points",
                        "mandatory_points": [{"text": "p"}] * (MAX_POINTS + 1),
                    }
                ],
                id="too-many-points",
            ),
            pytest.param(
                [
                    {
                        "check_method": "mandatory_points",
                        "mandatory_points": [{"text": "x" * (MAX_POINT_CHARS + 1)}],
                    }
                ],
                id="point-text",
            ),
            pytest.param(
                [{"concepts": ["Recursion"] * (MAX_CONCEPTS + 1)}],
                id="too-many-concepts",
            ),
        ],
    )
    def test_the_limits_hold_at_the_boundary(
        self, criteria: list[dict[str, Any]]
    ) -> None:
        """Decision 17's limits, the stored form's, hold for the request."""
        body = {
            "criteria": [
                {**_edit().model_dump(mode="json"), **changes} for changes in criteria
            ]
        }

        with pytest.raises(ValidationError):
            CriteriaOverrideRequest.model_validate(body)

    def test_the_limits_themselves_are_accepted(self) -> None:
        """The premise of the test above: one past the limit fails, not the limit."""
        body = {
            "criteria": [
                {
                    **_edit().model_dump(mode="json"),
                    "text": "x" * MAX_TEXT_CHARS,
                    "check_method": "mandatory_points",
                    "mandatory_points": [{"text": "x" * MAX_POINT_CHARS}] * MAX_POINTS,
                    "concepts": ["Recursion"] * MAX_CONCEPTS,
                }
            ]
            * MAX_CRITERIA
        }

        assert len(CriteriaOverrideRequest.model_validate(body).criteria) == (
            MAX_CRITERIA
        )


class TestTheReadme:
    """The README's account of the routes, held to the application."""

    _README = (
        pathlib.Path(__file__).parents[3]
        / "src"
        / "course_supporter"
        / "homework"
        / "README.md"
    )

    def test_the_documented_criteria_routes_are_the_served_ones(self) -> None:
        text = self._README.read_text(encoding="utf-8")
        documented = {
            path.replace("$DOC_ID", "{document_id}")
            for path in re.findall(r"/api/v1/documents/\$DOC_ID/criteria\S*", text)
        }
        served = {
            # ``app.routes`` is typed ``BaseRoute``, which has no ``path``; the
            # filter keeps only routes that carry one.
            route.path  # type: ignore[attr-defined]
            for route in app.routes
            if "/criteria" in getattr(route, "path", "")
        }

        assert documented, "the document shows some requests"
        assert served, "the application serves some criteria routes"
        assert documented == served, f"documented {documented}, served {served}"

    def test_every_refusal_code_of_the_criteria_routes_is_documented(self) -> None:
        text = self._README.read_text(encoding="utf-8")
        real = {code.value for code in CriteriaRefusalCode}

        assert real, "the vocabulary under test is not empty"
        undocumented = sorted(code for code in real if f"`{code}`" not in text)
        assert not undocumented, f"undocumented: {undocumented}"
