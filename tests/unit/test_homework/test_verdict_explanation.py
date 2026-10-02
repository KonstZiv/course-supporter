"""The explanation's facts and its check against them (mentor-rebuild 09b, K5).

``TASK.md`` lock 7: an explanation that says "passed" of a work that is not,
or leaves an unmet "must" criterion without its remark, does not pass the
check. ``PRE-FLIGHT.md`` 9.7 widens the rule the lock names: exactly one
remark on every unmet criterion of any weight, none on a met one or on a
point, and the pass repeated as the code counted it — the other way round is
refused too.

The facts come from the stored rows: the score and the pass by the
evaluation's own functions, each item with only the fields its verdict uses —
a quote that was not found is never shown, a "met" the safeguard kept reads
"met".
"""

from __future__ import annotations

import doctest
import json
from typing import Any

import pytest

from course_supporter.criteria_kinds import VerdictItemKind, VerdictValue
from course_supporter.homework import verdict_explanation
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_verdicts import QuotePlace
from course_supporter.homework.verdict_explanation import (
    ExplanationFacts,
    JudgedItem,
    explanation_facts,
    read_explanation,
)
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.storage.orm import SubmissionCriterionVerdict


def _criterion(cid: str, weight: str, points: int = 0) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": "mandatory_points" if points else "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [
                {"id": f"{cid}.p{n}", "text": f"Point {n} of {cid}"}
                for n in range(1, points + 1)
            ],
        }
    )


# c1 "must" on its own, c2 "should" by two points, c3 "may" on its own.
_CRITERIA = (
    _criterion("c1", "must"),
    _criterion("c2", "should", points=2),
    _criterion("c3", "may"),
)


def _row(
    item_id: str,
    verdict: str,
    *,
    weight: str | None = None,
    quote: str | None = None,
    quote_file: str | None = None,
    lines: tuple[int, int] | None = None,
    missing: str | None = None,
    quote_not_found: bool = False,
    safeguard_fired: bool = False,
) -> SubmissionCriterionVerdict:
    """A stored row as the repository reads it back; no session needed."""
    kind = VerdictItemKind.POINT if "." in item_id else VerdictItemKind.CRITERION
    first, last = lines if lines else (None, None)
    return SubmissionCriterionVerdict(
        item_id=item_id,
        item_kind=kind.value,
        weight=weight,
        verdict=verdict,
        quote=quote,
        quote_file=quote_file,
        quote_line_start=first,
        quote_line_end=last,
        missing=missing,
        quote_not_found=quote_not_found,
        safeguard_fired=safeguard_fired,
    )


def _rows(c1: str = "met", p2: str = "not_met") -> list[SubmissionCriterionVerdict]:
    """Rows of the three criteria; c2 is met only when both its points are."""
    c2 = "met" if p2 == "met" else "not_met"
    return [
        _row(
            "c1",
            c1,
            weight="must",
            quote="def fibonacci(n):" if c1 == "met" else None,
            quote_file="solution.py" if c1 == "met" else None,
            lines=(1, 1) if c1 == "met" else None,
            missing=None if c1 == "met" else "Немає функції.",
        ),
        _row("c2", c2, weight="should"),
        _row(
            "c2.p1",
            "met",
            quote="return fibonacci(n - 1)",
            quote_file="solution.py",
            lines=(4, 4),
        ),
        _row(
            "c2.p2",
            p2,
            quote="assert fibonacci(10) == 55" if p2 == "met" else None,
            quote_file="test.py" if p2 == "met" else None,
            lines=(2, 2) if p2 == "met" else None,
            missing=None if p2 == "met" else "Немає жодного тесту.",
        ),
        _row(
            "c3",
            "met",
            weight="may",
            quote="Пояснення в README.",
            quote_file="README.docx",
        ),
    ]


def _answer(
    *,
    passed: bool,
    remarks: list[str],
    why: str = "Розв'язок працює.",
    mentor_voice: str | None = None,
    **overrides: Any,
) -> str:
    body: dict[str, Any] = {
        "passed": passed,
        "why": why,
        "remarks": [
            {
                "id": cid,
                "what": "Зараз цього немає.",
                "why": "Без цього розв'язок не перевірено.",
                "todo": "Додайте тест на базовий випадок.",
            }
            for cid in remarks
        ],
        "mentor_voice": mentor_voice,
    }
    body.update(overrides)
    return json.dumps(body, ensure_ascii=False)


def _refusal(content: str, facts: ExplanationFacts) -> str:
    with pytest.raises(StructuralRetryError) as caught:
        read_explanation(content, facts)
    return caught.value.feedback


class TestTheFacts:
    def test_the_score_and_the_pass_are_the_evaluations_own(self) -> None:
        """Weights 3 / 2 / 1: c1 and c3 met, c2 not — (3 + 1) of 6, rounded down."""
        facts = explanation_facts(_CRITERIA, _rows())

        assert facts.passed is True
        assert facts.score == 66
        assert facts.unmet == ("c2",)

    def test_an_unmet_must_fails_the_pass(self) -> None:
        facts = explanation_facts(_CRITERIA, _rows(c1="not_met"))

        assert facts.passed is False
        assert facts.score == 16
        assert facts.unmet == ("c1", "c2")

    def test_everything_met_leaves_nothing_unmet(self) -> None:
        facts = explanation_facts(_CRITERIA, _rows(p2="met"))

        assert (facts.passed, facts.score, facts.unmet) == (True, 100, ())

    def test_each_criterion_carries_its_verdict_in_the_lists_order(self) -> None:
        facts = explanation_facts(_CRITERIA, _rows())

        c1, c2, c3 = facts.criteria
        assert [c.criterion.id for c in facts.criteria] == ["c1", "c2", "c3"]
        assert c1.judged == JudgedItem(
            id="c1",
            verdict=VerdictValue.MET,
            quote="def fibonacci(n):",
            place=QuotePlace("solution.py", (1, 1)),
        )
        assert c1.points == ()
        assert c2.verdict is VerdictValue.NOT_MET
        assert c2.judged is None
        assert c2.points == (
            JudgedItem(
                id="c2.p1",
                verdict=VerdictValue.MET,
                quote="return fibonacci(n - 1)",
                place=QuotePlace("solution.py", (4, 4)),
            ),
            JudgedItem(
                id="c2.p2",
                verdict=VerdictValue.NOT_MET,
                missing="Немає жодного тесту.",
            ),
        )
        # A document has no lines: the place is its file alone.
        assert c3.judged is not None
        assert c3.judged.place == QuotePlace("README.docx", None)

    def test_a_quote_that_was_not_found_is_never_shown(self) -> None:
        """The row keeps the model's quote; the facts do not carry it on."""
        rows = _rows()
        rows[3] = _row(
            "c2.p2",
            "not_met",
            quote="assert fibonaci(10) == 55",
            quote_not_found=True,
        )

        (point,) = [
            p
            for p in explanation_facts(_CRITERIA, rows).criteria[1].points
            if p.id == "c2.p2"
        ]

        assert point == JudgedItem(
            id="c2.p2", verdict=VerdictValue.NOT_MET, quote_not_found=True
        )

    def test_a_met_the_safeguard_kept_reads_met(self) -> None:
        """The kept row carries quote_not_found as well (K2): it is not shown."""
        rows = _rows()
        rows[0] = _row(
            "c1",
            "met",
            weight="must",
            quote="def fibonacci(n):",
            quote_file="solution.py",
            lines=(1, 1),
            quote_not_found=True,
            safeguard_fired=True,
        )

        judged = explanation_facts(_CRITERIA, rows).criteria[0].judged

        assert judged is not None
        assert judged.verdict is VerdictValue.MET
        assert judged.kept_from_earlier is True
        assert judged.quote_not_found is False
        assert judged.quote == "def fibonacci(n):"

    @pytest.mark.parametrize(
        ("change", "fault"),
        [
            pytest.param(
                lambda rows: rows[:-1], "criterion c3 has no verdict row", id="no-row"
            ),
            pytest.param(
                lambda rows: [r for r in rows if r.item_id != "c2.p2"],
                "point c2.p2 has no verdict row",
                id="no-point-row",
            ),
            pytest.param(
                lambda rows: [*rows, _row("c9", "met", weight="may")],
                r"does not have: \['c9'\]",
                id="a-foreign-row",
            ),
            pytest.param(
                lambda rows: [*rows, _row("c1", "met", weight="must")],
                r"repeat items \['c1'\]",
                id="a-repeated-row",
            ),
            pytest.param(
                lambda rows: [
                    _row("c1.p1", "met") if r.item_id == "c1" else r for r in rows
                ],
                "criterion c1 has no verdict row",
                id="a-row-of-the-wrong-kind",
            ),
        ],
    )
    def test_rows_of_another_list_are_a_defect(self, change: Any, fault: str) -> None:
        with pytest.raises(ValueError, match=fault):
            explanation_facts(_CRITERIA, change(_rows()))


class TestThePassIsRepeated:
    """Lock 7, first half: "passed" said of a work that is not is refused."""

    def test_passed_said_of_a_work_that_is_not_is_refused(self) -> None:
        facts = explanation_facts(_CRITERIA, _rows(c1="not_met"))

        feedback = _refusal(_answer(passed=True, remarks=["c1", "c2"]), facts)

        assert feedback.startswith("passed must be false:")

    def test_not_passed_said_of_a_work_that_is_is_refused(self) -> None:
        facts = explanation_facts(_CRITERIA, _rows())

        feedback = _refusal(_answer(passed=False, remarks=["c2"]), facts)

        assert feedback.startswith("passed must be true:")

    def test_the_pass_repeated_rightly_is_read(self) -> None:
        facts = explanation_facts(_CRITERIA, _rows(c1="not_met"))

        answer = read_explanation(_answer(passed=False, remarks=["c1", "c2"]), facts)

        assert answer.passed is False
        assert [r.id for r in answer.remarks] == ["c1", "c2"]


class TestEveryUnmetCriterionHasItsRemark:
    """Lock 7, second half: no unmet criterion goes without its remark."""

    def test_an_unmet_must_without_its_remark_is_refused(self) -> None:
        facts = explanation_facts(_CRITERIA, _rows(c1="not_met"))

        feedback = _refusal(_answer(passed=False, remarks=["c2"]), facts)

        assert "missing ['c1'], unexpected [], repeated []" in feedback

    def test_an_unmet_should_without_its_remark_is_refused(self) -> None:
        """``PRE-FLIGHT.md`` 9.7: every unmet criterion, whatever its weight."""
        facts = explanation_facts(_CRITERIA, _rows())

        feedback = _refusal(_answer(passed=True, remarks=[]), facts)

        assert "missing ['c2']" in feedback

    @pytest.mark.parametrize(
        ("remarks", "fault"),
        [
            pytest.param(["c2", "c3"], "unexpected ['c3']", id="on-a-met-criterion"),
            pytest.param(["c2", "c2.p2"], "unexpected ['c2.p2']", id="on-a-point"),
            pytest.param(["c2", "c9"], "unexpected ['c9']", id="on-no-criterion"),
            pytest.param(["c2", "c2"], "repeated ['c2']", id="twice"),
        ],
    )
    def test_a_remark_where_none_belongs_is_refused(
        self, remarks: list[str], fault: str
    ) -> None:
        facts = explanation_facts(_CRITERIA, _rows())

        feedback = _refusal(_answer(passed=True, remarks=remarks), facts)

        assert fault in feedback
        assert "Give one remark for each of ['c2'] and no others." in feedback

    def test_everything_met_takes_no_remark(self) -> None:
        facts = explanation_facts(_CRITERIA, _rows(p2="met"))

        answer = read_explanation(_answer(passed=True, remarks=[]), facts)

        assert answer.remarks == ()


class TestTheTextsAreThere:
    @pytest.mark.parametrize("blank", ["", "  \n "])
    def test_an_empty_reason_or_remark_part_is_refused(self, blank: str) -> None:
        facts = explanation_facts(_CRITERIA, _rows())
        body = json.loads(_answer(passed=True, remarks=["c2"], why=blank))
        body["remarks"][0]["todo"] = blank

        feedback = _refusal(json.dumps(body), facts)

        assert "these texts are empty: ['why', 'remarks[c2].todo']" in feedback

    def test_no_word_of_the_mentors_own_is_a_null(self) -> None:
        facts = explanation_facts(_CRITERIA, _rows())

        answer = read_explanation(_answer(passed=True, remarks=["c2"]), facts)

        assert answer.mentor_voice is None

    def test_every_disagreement_is_named_at_once(self) -> None:
        facts = explanation_facts(_CRITERIA, _rows(c1="not_met"))

        feedback = _refusal(_answer(passed=True, remarks=["c3"], why=" "), facts)

        assert "passed must be false" in feedback
        assert "missing ['c1', 'c2'], unexpected ['c3']" in feedback
        assert "these texts are empty: ['why']" in feedback
        assert feedback.endswith("Regenerate the whole JSON object.")


class TestTheForm:
    @pytest.mark.parametrize(
        ("content", "fault"),
        [
            pytest.param("not json", "Invalid JSON", id="not-json"),
            pytest.param(
                json.dumps({"passed": True, "why": "x", "remarks": []}),
                "field: mentor_voice",
                id="a-field-missing",
            ),
            pytest.param(
                _answer(passed=True, remarks=["c2"], read=[]),
                "field: read",
                id="a-field-it-does-not-have",
            ),
            pytest.param(
                json.dumps(
                    {
                        "passed": True,
                        "why": "x",
                        "remarks": [{"id": "c2", "what": "a", "why": "b"}],
                        "mentor_voice": None,
                    }
                ),
                "field: remarks.0.todo",
                id="a-remark-without-its-third-part",
            ),
        ],
    )
    def test_an_answer_of_another_form_is_refused(
        self, content: str, fault: str
    ) -> None:
        facts = explanation_facts(_CRITERIA, _rows())

        assert fault in _refusal(content, facts)


def test_the_examples_in_the_docstrings_hold() -> None:
    """The gate does not collect doctests; this runs them."""
    result = doctest.testmod(verdict_explanation)

    assert result.attempted > 0
    assert result.failed == 0
