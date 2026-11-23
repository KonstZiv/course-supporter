"""The explanation sees its own earlier remark, and the code's limits of examples.

Task 13, explanation side (the product owner's decisions A and B of
2026-11-23). Without a database:

* ``previous_remark`` is shown only beside a criterion whose remark is
  ``open`` — and only when the earlier attempt exists, has an explanation,
  written in the language of this request, with a remark of that criterion's
  id: one test for each condition that takes it away;
* never on a point, and never on a first attempt;
* the limits are ``N // 2`` for the review and ``N * 3 // 10`` for one answer
  to a reply, from the number of criteria of the list;
* ``reply_example_limit`` goes only with the replies, and an input without
  limits — the project's — shows none.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

import pytest

from course_supporter.agents import review_explainer
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.earlier_review import EarlierReview, previous_remarks
from course_supporter.homework.student_replies import RepliesView
from course_supporter.homework.verdict_explanation import (
    CriterionRemark,
    ExampleLimits,
    PreviousRemark,
    example_limits,
    explanation_facts,
)

_LANGUAGE = "ukr"


@dataclass(frozen=True)
class _Stated:
    """A verdict row of this attempt, as far as the rule reads it."""

    item_id: str
    item_kind: str
    remark_state: str | None


def _remark(cid: str) -> CriterionRemark:
    return CriterionRemark(
        id=cid,
        what=f"Як зараз у {cid}.",
        why=f"Чому це важливо для {cid}.",
        todo=f"Що зробити з {cid}.",
        links=(),
    )


def _earlier(
    *ids: str, language: str | None = _LANGUAGE, explained: bool = True
) -> EarlierReview:
    return EarlierReview(
        submission_id=uuid.uuid4(),
        rows=(),
        language=language if explained else None,
        remarks=MappingProxyType(
            {cid: _remark(cid) for cid in ids} if explained else {}
        ),
    )


_OPEN = _Stated("c1", "criterion", "open")


class TestTheRemarkMadeBefore:
    def test_an_open_criterion_with_its_remark_is_shown_it(self) -> None:
        shown = previous_remarks([_OPEN], _earlier("c1"), language=_LANGUAGE)

        assert shown == {
            "c1": PreviousRemark(
                what="Як зараз у c1.",
                why="Чому це важливо для c1.",
                todo="Що зробити з c1.",
            )
        }

    def test_no_earlier_attempt_shows_none(self) -> None:
        assert previous_remarks([_OPEN], None, language=_LANGUAGE) == {}

    def test_an_earlier_attempt_without_an_explanation_shows_none(self) -> None:
        earlier = _earlier("c1", explained=False)

        assert previous_remarks([_OPEN], earlier, language=_LANGUAGE) == {}

    def test_an_explanation_in_another_language_shows_none(self) -> None:
        earlier = _earlier("c1", language="eng")

        assert previous_remarks([_OPEN], earlier, language=_LANGUAGE) == {}

    def test_no_remark_with_the_criterions_id_shows_none(self) -> None:
        earlier = _earlier("c2")

        assert previous_remarks([_OPEN], earlier, language=_LANGUAGE) == {}

    @pytest.mark.parametrize("state", ["new", "fixed", "broken", None])
    def test_a_state_other_than_open_shows_none(self, state: str | None) -> None:
        row = _Stated("c1", "criterion", state)

        assert previous_remarks([row], _earlier("c1"), language=_LANGUAGE) == {}

    def test_a_point_is_never_shown_one(self) -> None:
        """A point's state is its own; the remark is the criterion's."""
        point = _Stated("c1.p1", "point", "open")
        earlier = _earlier("c1", "c1.p1")

        assert previous_remarks([point], earlier, language=_LANGUAGE) == {}

    def test_only_the_open_ones_of_many(self) -> None:
        rows = [
            _Stated("c1", "criterion", "open"),
            _Stated("c2", "criterion", "new"),
            _Stated("c3", "criterion", "broken"),
            _Stated("c4", "criterion", "open"),
            _Stated("c5", "criterion", None),
        ]
        earlier = _earlier("c1", "c2", "c3", "c4", "c5")

        assert set(previous_remarks(rows, earlier, language=_LANGUAGE)) == {"c1", "c4"}


class TestTheLimitsOfExamples:
    @pytest.mark.parametrize(
        ("criteria", "review", "reply"),
        [(1, 0, 0), (2, 1, 0), (3, 1, 0), (4, 2, 1), (10, 5, 3)],
    )
    def test_they_are_counted_from_the_criteria_rounded_down(
        self, criteria: int, review: int, reply: int
    ) -> None:
        assert criteria // 2 == review
        assert criteria * 3 // 10 == reply
        assert example_limits(criteria) == ExampleLimits(review=review, reply=reply)


# ── The request (render_context) ──


def _criterion(cid: str, points: int = 0) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": "should",
            "check_method": "mandatory_points" if points else "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [
                {"id": f"{cid}.p{n}", "text": f"Point {n} of {cid}"}
                for n in range(1, points + 1)
            ],
        }
    )


@dataclass(frozen=True)
class _Row:
    item_id: str
    item_kind: str
    weight: str | None
    verdict: str
    quote: str | None = None
    quote_file: str | None = None
    quote_line_start: int | None = None
    quote_line_end: int | None = None
    missing: str | None = None
    quote_not_found: bool = False
    safeguard_fired: bool = False
    quote_from_base: bool = False


_CRITERIA = (_criterion("c1"), _criterion("c2", points=2), _criterion("c3"))
_ROWS = (
    _Row("c1", "criterion", "should", "not_met", missing="Немає c1."),
    _Row("c2", "criterion", "should", "not_met"),
    _Row("c2.p1", "point", None, "not_met", missing="Немає першого пункту."),
    _Row("c2.p2", "point", None, "met", quote="x = compute(1)", quote_file="a.py"),
    _Row("c3", "criterion", "should", "met", quote="y = compute(2)", quote_file="a.py"),
)


def _shown(**fields: Any) -> review_explainer.ExplanationInput:
    return review_explainer.ExplanationInput(
        task_title="T",
        task_description="D",
        task_text="X",
        facts=explanation_facts(_CRITERIA, _ROWS, layer=CriteriaLayer.MODEL),
        submission_text="--- a.py ---\nx = compute(1)\n",
        language="Ukrainian",
        course_links={},
        **fields,
    )


_REMARK = PreviousRemark(what="Було так.", why="Бо так.", todo="Зробіть так.")


class TestTheRequest:
    def test_a_first_attempt_has_no_remark_made_before_anywhere(self) -> None:
        context = review_explainer.render_context(
            _shown(example_limits=example_limits(len(_CRITERIA)))
        )

        assert "previous_remark" not in json.dumps(context, ensure_ascii=False)
        assert context["result"]["example_limit"] == 1
        assert "reply_example_limit" not in context["result"]

    def test_the_remark_stands_on_its_criterion_and_never_on_a_point(self) -> None:
        context = review_explainer.render_context(
            _shown(
                example_limits=example_limits(len(_CRITERIA)),
                previous_remarks={"c2": _REMARK},
            )
        )

        c1, c2, c3 = context["criteria"]
        assert c2["previous_remark"] == {
            "what": "Було так.",
            "why": "Бо так.",
            "todo": "Зробіть так.",
        }
        assert all("previous_remark" not in point for point in c2["points"])
        assert "previous_remark" not in c1
        assert "previous_remark" not in c3

    def test_the_limit_of_a_reply_goes_with_the_replies_alone(self) -> None:
        shown = _shown(example_limits=ExampleLimits(review=5, reply=3))
        with_replies = replace(shown, replies=RepliesView(threads=(), to_answer=()))

        assert review_explainer.render_context(shown)["result"] == {
            "passed": False,
            "score": 33,
            "example_limit": 5,
        }
        assert review_explainer.render_context(with_replies)["result"] == {
            "passed": False,
            "score": 33,
            "example_limit": 5,
            "reply_example_limit": 3,
        }

    def test_an_input_without_limits_shows_none_as_before(self) -> None:
        """The project's stage builds it so: its request stays what it was."""
        context = review_explainer.render_context(_shown())

        assert context["result"] == {"passed": False, "score": 33}
        assert "previous_remark" not in json.dumps(context, ensure_ascii=False)
