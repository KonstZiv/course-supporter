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
from typing import Any, ClassVar

import pytest

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
)
from course_supporter.homework import verdict_explanation
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_verdicts import (
    QuotePlace,
    is_passed,
    score_percent,
)
from course_supporter.homework.verdict_explanation import (
    CriterionRemark,
    ExplanationAnswer,
    ExplanationFacts,
    JudgedItem,
    drop_unshown_links,
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


# c1 "must" on its own, c2 "may" by two points, c3 "should" on its own: with
# c2 alone unmet the work scores 83 and passes on any list (the pass rule of
# 2026-11-12: 80 on the model's list, every "must" and 70 on the author's).
_CRITERIA = (
    _criterion("c1", "must"),
    _criterion("c2", "may", points=2),
    _criterion("c3", "should"),
)


def _facts(
    rows: list[SubmissionCriterionVerdict],
    layer: CriteriaLayer = CriteriaLayer.MODEL,
) -> ExplanationFacts:
    return explanation_facts(_CRITERIA, rows, layer=layer)


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
        _row("c2", c2, weight="may"),
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
            weight="should",
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
    links: dict[str, list[str]] | None = None,
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
                "links": (links or {}).get(cid, []),
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
        """Weights 3 / 2 / 1: c1 and c3 met, c2 not — (3 + 2) of 6, rounded down."""
        rows = _rows()

        for layer in CriteriaLayer:
            facts = _facts(rows, layer)
            assert facts.passed is is_passed(rows, layer=layer) is True
            assert facts.score == score_percent(rows) == 83
            assert facts.unmet == ("c2",)

    def test_the_pass_is_the_rule_of_the_lists_layer(self) -> None:
        """The pass rule of 2026-11-12, as the facts tell it to the model.

        c1 (the "must") and c2 met, c3 not: (3 + 1) of 6 is 66 — below both
        thresholds; the facts say what the rule says, on either list.
        """
        rows = _rows(p2="met")
        rows[-1] = _row("c3", "not_met", weight="should", missing="Немає README.")

        for layer in CriteriaLayer:
            facts = _facts(rows, layer)
            assert facts.score == 66
            assert facts.passed is is_passed(rows, layer=layer) is False

    def test_an_unmet_must_fails_the_pass(self) -> None:
        facts = _facts(_rows(c1="not_met"))

        assert facts.passed is False
        assert facts.score == 33
        assert facts.unmet == ("c1", "c2")

    def test_everything_met_leaves_nothing_unmet(self) -> None:
        facts = _facts(_rows(p2="met"))

        assert (facts.passed, facts.score, facts.unmet) == (True, 100, ())

    def test_each_criterion_carries_its_verdict_in_the_lists_order(self) -> None:
        facts = _facts(_rows())

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

        (point,) = [p for p in _facts(rows).criteria[1].points if p.id == "c2.p2"]

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

        judged = _facts(rows).criteria[0].judged

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
            _facts(change(_rows()))


class TestThePassIsRepeated:
    """Lock 7, first half: "passed" said of a work that is not is refused."""

    def test_passed_said_of_a_work_that_is_not_is_refused(self) -> None:
        facts = _facts(_rows(c1="not_met"))

        feedback = _refusal(_answer(passed=True, remarks=["c1", "c2"]), facts)

        assert feedback.startswith("passed must be false:")

    def test_not_passed_said_of_a_work_that_is_is_refused(self) -> None:
        facts = _facts(_rows())

        feedback = _refusal(_answer(passed=False, remarks=["c2"]), facts)

        assert feedback.startswith("passed must be true:")

    def test_the_pass_repeated_rightly_is_read(self) -> None:
        facts = _facts(_rows(c1="not_met"))

        answer = read_explanation(_answer(passed=False, remarks=["c1", "c2"]), facts)

        assert answer.passed is False
        assert [r.id for r in answer.remarks] == ["c1", "c2"]


class TestEveryUnmetCriterionHasItsRemark:
    """Lock 7, second half: no unmet criterion goes without its remark."""

    def test_an_unmet_must_without_its_remark_is_refused(self) -> None:
        facts = _facts(_rows(c1="not_met"))

        feedback = _refusal(_answer(passed=False, remarks=["c2"]), facts)

        assert "missing ['c1'], unexpected [], repeated []" in feedback

    def test_an_unmet_should_without_its_remark_is_refused(self) -> None:
        """``PRE-FLIGHT.md`` 9.7: every unmet criterion, whatever its weight."""
        facts = _facts(_rows())

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
        facts = _facts(_rows())

        feedback = _refusal(_answer(passed=True, remarks=remarks), facts)

        assert fault in feedback
        assert "Give one remark for each of ['c2'] and no others." in feedback

    def test_everything_met_takes_no_remark(self) -> None:
        facts = _facts(_rows(p2="met"))

        answer = read_explanation(_answer(passed=True, remarks=[]), facts)

        assert answer.remarks == ()


class TestTheTextsAreThere:
    @pytest.mark.parametrize("blank", ["", "  \n "])
    def test_an_empty_reason_or_remark_part_is_refused(self, blank: str) -> None:
        facts = _facts(_rows())
        body = json.loads(_answer(passed=True, remarks=["c2"], why=blank))
        body["remarks"][0]["todo"] = blank

        feedback = _refusal(json.dumps(body), facts)

        assert "these texts are empty: ['why', 'remarks[c2].todo']" in feedback

    def test_no_word_of_the_mentors_own_is_a_null(self) -> None:
        facts = _facts(_rows())

        answer = read_explanation(_answer(passed=True, remarks=["c2"]), facts)

        assert answer.mentor_voice is None

    def test_every_disagreement_is_named_at_once(self) -> None:
        facts = _facts(_rows(c1="not_met"))

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
                        "remarks": [{"id": "c2", "what": "a", "why": "b", "links": []}],
                        "mentor_voice": None,
                    }
                ),
                "field: remarks.0.todo",
                id="a-remark-without-its-third-part",
            ),
            pytest.param(
                json.dumps(
                    {
                        "passed": True,
                        "why": "x",
                        "remarks": [{"id": "c2", "what": "a", "why": "b", "todo": "c"}],
                        "mentor_voice": None,
                    }
                ),
                "field: remarks.0.links",
                id="a-remark-of-the-model-without-its-links",
            ),
        ],
    )
    def test_an_answer_of_another_form_is_refused(
        self, content: str, fault: str
    ) -> None:
        facts = _facts(_rows())

        assert fault in _refusal(content, facts)


class TestTheLinks:
    """Task 10, K5: the course links an answer names, and what is kept of them."""

    def test_links_are_read_as_given(self) -> None:
        facts = _facts(_rows(c1="not_met"))

        answer = read_explanation(
            _answer(
                passed=False, remarks=["c1", "c2"], links={"c1": ["c1.l2", "c1.l1"]}
            ),
            facts,
        )

        assert [r.links for r in answer.remarks] == [("c1.l2", "c1.l1"), ()]

    def test_a_made_up_link_is_no_disagreement(self) -> None:
        """A link is not a fact: it is cut later, never paid for with a retry."""
        facts = _facts(_rows())

        answer = read_explanation(
            _answer(passed=True, remarks=["c2"], links={"c2": ["c9.l7"]}), facts
        )

        assert answer.remarks[0].links == ("c9.l7",)

    def test_a_body_kept_before_links_reads_as_none(self) -> None:
        """A body stored before task 10, read as the review's builder reads it."""
        kept = {
            "passed": True,
            "why": "x",
            "remarks": [{"id": "c2", "what": "a", "why": "b", "todo": "c"}],
            "mentor_voice": None,
        }

        answer = ExplanationAnswer.model_validate(kept)

        assert answer.remarks[0].links == ()

    def test_a_kept_body_round_trips_its_links(self) -> None:
        remark = CriterionRemark(id="c2", what="a", why="b", todo="c", links=("c2.l1",))
        answer = ExplanationAnswer(
            passed=True, why="x", remarks=(remark,), mentor_voice=None
        )

        assert ExplanationAnswer.model_validate(answer.model_dump(mode="json")) == (
            answer
        )


def _remark(cid: str, *links: str) -> CriterionRemark:
    return CriterionRemark(id=cid, what="a", why="b", todo="c", links=links)


def _with(*remarks: CriterionRemark) -> ExplanationAnswer:
    return ExplanationAnswer(passed=False, why="x", remarks=remarks, mentor_voice=None)


class TestOnlyShownLinksAreKept:
    """Lock h (task 10, K5): a link not shown for ITS criterion is cut."""

    _SHOWN: ClassVar[dict[str, set[str]]] = {
        "c1": {"c1.l1", "c1.l2"},
        "c2": {"c2.l1"},
    }

    def test_a_shown_link_is_kept_in_the_models_order(self) -> None:
        answer = _with(_remark("c1", "c1.l2", "c1.l1"), _remark("c2", "c2.l1"))

        kept, cut = drop_unshown_links(answer, self._SHOWN)

        assert kept == answer
        assert cut == {}

    def test_a_link_shown_for_another_criterion_is_cut(self) -> None:
        answer = _with(_remark("c1", "c1.l1", "c2.l1"), _remark("c2"))

        kept, cut = drop_unshown_links(answer, self._SHOWN)

        assert [r.links for r in kept.remarks] == [("c1.l1",), ()]
        assert cut == {"c1": ("c2.l1",)}

    def test_a_link_shown_for_none_is_cut(self) -> None:
        answer = _with(_remark("c1", "c1.l3", "c1.l1"), _remark("c2", "c2.l9"))

        kept, cut = drop_unshown_links(answer, self._SHOWN)

        assert [r.links for r in kept.remarks] == [("c1.l1",), ()]
        assert cut == {"c1": ("c1.l3",), "c2": ("c2.l9",)}

    def test_a_criterion_shown_nothing_keeps_nothing(self) -> None:
        answer = _with(_remark("c3", "c3.l1"))

        kept, cut = drop_unshown_links(answer, self._SHOWN)

        assert kept.remarks[0].links == ()
        assert cut == {"c3": ("c3.l1",)}

    def test_a_repeated_link_is_kept_once_and_is_no_fabrication(self) -> None:
        answer = _with(_remark("c1", "c1.l1", "c1.l1"))

        kept, cut = drop_unshown_links(answer, self._SHOWN)

        assert kept.remarks[0].links == ("c1.l1",)
        assert cut == {}

    def test_the_texts_are_left_as_they_are(self) -> None:
        answer = _with(_remark("c1", "c5.l1"))

        kept, _ = drop_unshown_links(answer, self._SHOWN)

        remark = kept.remarks[0]
        assert (remark.id, remark.what, remark.why, remark.todo) == (
            "c1",
            "a",
            "b",
            "c",
        )
        assert (kept.passed, kept.why, kept.mentor_voice) == (False, "x", None)


def test_the_examples_in_the_docstrings_hold() -> None:
    """The gate does not collect doctests; this runs them."""
    result = doctest.testmod(verdict_explanation)

    assert result.attempted > 0
    assert result.failed == 0
