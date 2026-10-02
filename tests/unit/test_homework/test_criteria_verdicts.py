"""Unit tests for the verdicts on criteria (mentor-rebuild task 09b, K2).

Pure functions, no session and no model. Locks of ``TASK.md`` section 5 held
here: 1 (the score against a count by hand), 2 (passed ⇔ every "must"), 3 (a
criterion with points ⇔ every point), the pure part of 4 (a quote the work does
not have gives no "met"), the pure part of 5 (the safeguard, within one list
only), 9 (the same input, the same result), and the item-level repeat ratified
2026-10-02 (a defect of one verdict repeats that verdict alone, never the whole
answer; the field a verdict does not use is ignored). Every score below is counted by
hand in the comment beside it.
"""

from __future__ import annotations

import doctest
import json
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework import criteria_verdicts
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_list_service import CriteriaInForce
from course_supporter.homework.criteria_verdicts import (
    MAX_QUOTE_CHARS,
    MIN_QUOTE_CHARS,
    EvaluationAnswer,
    QuotePlace,
    RepeatItem,
    RepeatReason,
    SubmittedWork,
    WorkFile,
    is_passed,
    items_to_ask_again,
    judged_items,
    read_answer,
    score_percent,
    settle_verdicts,
)
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.storage.orm import SubmissionCriterionVerdict
from course_supporter.storage.submission_verdict_repository import VerdictRecord

MET = VerdictValue.MET
NOT_MET = VerdictValue.NOT_MET

_BOM = chr(0xFEFF)
_NBSP = chr(0xA0)

_LIST_ID = uuid.UUID("01999999-0000-7000-8000-000000000001")
_OTHER_LIST_ID = uuid.UUID("01999999-0000-7000-8000-000000000002")
_EARLIER_SUBMISSION = uuid.UUID("01999999-0000-7000-8000-0000000000e1")


def _criterion(
    cid: str, weight: str, points: int = 0, *, method: str | None = None
) -> Criterion:
    check = method or ("mandatory_points" if points else "model_verdict")
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": check,
            "soft_descent": check == "code_test",
            "concepts": [],
            "mandatory_points": [
                {"id": f"{cid}.p{n}", "text": f"Point {n} of {cid}"}
                for n in range(1, points + 1)
            ],
        }
    )


def _in_force(
    *criteria: Criterion,
    layer: CriteriaLayer = CriteriaLayer.MODEL,
    source_id: uuid.UUID = _LIST_ID,
) -> CriteriaInForce:
    return CriteriaInForce(criteria=criteria, layer=layer, source_id=source_id)


# The list most tests judge against. Weights: c1 3, c2 3, c3 2, c4 1, c5 2 —
# 11 in all. c2 and c5 are checked by mandatory points.
_LIST = _in_force(
    _criterion("c1", "must"),
    _criterion("c2", "must", points=2),
    _criterion("c3", "should"),
    _criterion("c4", "may"),
    _criterion("c5", "should", points=3),
)

_SOLUTION = (
    "def fibonacci(n):\n"  # 1
    "    if n < 2:\n"  # 2
    "        return n\n"  # 3
    "    return fibonacci(n - 1) + fibonacci(n - 2)\n"  # 4
    "\n"  # 5
    "\n"  # 6
    "def test_fibonacci():\n"  # 7
    "    assert fibonacci(10) == 55\n"  # 8
)
_WORK = SubmittedWork([WorkFile("fibonacci.py", _SOLUTION, has_lines=True)])

_ABSENT = "return memo[n]"  # a quote the work does not have


def _met(item: str, quote: str) -> dict[str, Any]:
    return {"id": item, "verdict": "met", "quote": quote, "missing": None}


def _not_met(item: str, missing: str = "The work does not do this.") -> dict[str, Any]:
    return {"id": item, "verdict": "not_met", "quote": None, "missing": missing}


def _content(*verdicts: dict[str, Any]) -> str:
    return json.dumps({"verdicts": list(verdicts)})


def _answer(*verdicts: dict[str, Any]) -> EvaluationAnswer:
    """An answer as the stage gets it: read and checked by :func:`read_answer`."""
    return read_answer(_content(*verdicts), [v["id"] for v in verdicts])


def _settle(
    answer: EvaluationAnswer,
    repeat: EvaluationAnswer | None = None,
    *,
    criteria: CriteriaInForce = _LIST,
    work: SubmittedWork = _WORK,
    earlier: dict[str, Any] | None = None,
) -> dict[str, VerdictRecord]:
    records = settle_verdicts(
        criteria, answer, repeat, work=work, earlier=earlier or {}
    )
    return {record.item_id: record for record in records}


# Case A: c1 met (3); c2 not met — one of its two points is not (0); c3 met
# (2); c4 not met (0); c5 met — all three points are (2).
_CASE_A = _answer(
    _met("c1", "def fibonacci(n):"),
    _met("c2.p1", "return fibonacci(n - 1)"),
    _not_met("c2.p2"),
    _met("c3", "assert fibonacci(10) == 55"),
    _not_met("c4"),
    _met("c5.p1", "def test_fibonacci():"),
    _met("c5.p2", "fibonacci(n - 2)"),
    _met("c5.p3", "def fibonacci(n):"),
)

# Case B: both "must" met (3 + 3), nothing else — c5 lost one point.
_CASE_B = _answer(
    _met("c1", "def fibonacci(n):"),
    _met("c2.p1", "return fibonacci(n - 1)"),
    _met("c2.p2", "fibonacci(n - 2)"),
    _not_met("c3"),
    _not_met("c4"),
    _met("c5.p1", "def test_fibonacci():"),
    _not_met("c5.p2"),
    _met("c5.p3", "def fibonacci(n):"),
)

# Case C: everything met but c1, a "must": 3 + 2 + 1 + 2.
_CASE_C = _answer(
    _not_met("c1"),
    _met("c2.p1", "return fibonacci(n - 1)"),
    _met("c2.p2", "fibonacci(n - 2)"),
    _met("c3", "assert fibonacci(10) == 55"),
    _met("c4", "return fibonacci(n - 1)"),
    _met("c5.p1", "def test_fibonacci():"),
    _met("c5.p2", "fibonacci(n - 2)"),
    _met("c5.p3", "def fibonacci(n):"),
)

_ALL_MET = _answer(
    _met("c1", "def fibonacci(n):"),
    _met("c2.p1", "return fibonacci(n - 1)"),
    _met("c2.p2", "fibonacci(n - 2)"),
    _met("c3", "assert fibonacci(10) == 55"),
    _met("c4", "return fibonacci(n - 1)"),
    _met("c5.p1", "def test_fibonacci():"),
    _met("c5.p2", "fibonacci(n - 2)"),
    _met("c5.p3", "def fibonacci(n):"),
)

_NONE_MET = _answer(
    *(_not_met(item) for item in ("c1", "c2.p1", "c2.p2", "c3", "c4")),
    *(_not_met(item) for item in ("c5.p1", "c5.p2", "c5.p3")),
)


def _records(answer: EvaluationAnswer) -> tuple[VerdictRecord, ...]:
    return settle_verdicts(_LIST, answer, None, work=_WORK, earlier={})


class TestScore:
    """Lock 1: the score is the share of weights, counted by hand."""

    def test_case_a(self) -> None:
        # 3 + 2 + 2 = 7 of 11 -> 700 // 11 = 63 (63.6: to the nearest, 64).
        assert score_percent(_records(_CASE_A)) == 63

    def test_case_b(self) -> None:
        # 3 + 3 = 6 of 11 -> 600 // 11 = 54 (54.5: to the nearest, 55).
        assert score_percent(_records(_CASE_B)) == 54

    def test_case_c(self) -> None:
        # 3 + 2 + 1 + 2 = 8 of 11 -> 800 // 11 = 72 (72.7: to the nearest, 73).
        assert score_percent(_records(_CASE_C)) == 72

    def test_everything_met_is_100(self) -> None:
        assert score_percent(_records(_ALL_MET)) == 100

    def test_nothing_met_is_0(self) -> None:
        assert score_percent(_records(_NONE_MET)) == 0

    def test_a_met_may_among_heavy_weights_reads_0(self) -> None:
        # 1 of 34 * 3 + 1 = 103 -> 100 // 103 = 0 (PRE-FLIGHT.md 9.10).
        criteria = _in_force(
            *(_criterion(f"c{n}", "must") for n in range(1, 35)),
            _criterion("c35", "may"),
        )
        answer = _answer(
            *(_not_met(f"c{n}") for n in range(1, 35)),
            _met("c35", "def fibonacci(n):"),
        )
        records = settle_verdicts(criteria, answer, None, work=_WORK, earlier={})
        assert score_percent(records) == 0

    def test_points_count_only_through_their_criterion(self) -> None:
        # c2 has one of two points met and counts 0, not half of 3.
        records = _records(_CASE_A)
        assert {
            r.item_id: r.verdict for r in records if r.item_id.startswith("c2")
        } == {
            "c2": NOT_MET,
            "c2.p1": MET,
            "c2.p2": NOT_MET,
        }
        assert score_percent(records) == 63

    def test_stored_rows_give_the_score_the_records_gave(self) -> None:
        # The review's builder recounts from stored rows, whose words are
        # plain strings, not enum members.
        rows = [
            SubmissionCriterionVerdict(
                item_id=record.item_id,
                item_kind=record.item_kind.value,
                weight=record.weight.value if record.weight else None,
                verdict=record.verdict.value,
            )
            for record in _records(_CASE_A)
        ]
        assert score_percent(rows) == 63
        assert is_passed(rows) is False

    def test_no_criterion_has_no_score(self) -> None:
        point = VerdictRecord("c1.p1", VerdictItemKind.POINT, None, MET, MET)
        with pytest.raises(ValueError, match="no score"):
            score_percent([point])

    def test_a_criterion_without_weight_is_refused(self) -> None:
        criterion = VerdictRecord("c1", VerdictItemKind.CRITERION, None, MET, MET)
        with pytest.raises(ValueError, match="c1 has no weight"):
            score_percent([criterion])


class TestPassed:
    """Lock 2: passed ⇔ every "must" criterion is met."""

    def test_every_must_met_passes_whatever_the_score(self) -> None:
        records = _records(_CASE_B)
        assert score_percent(records) == 54
        assert is_passed(records) is True

    def test_one_must_not_met_fails_with_everything_else_met(self) -> None:
        records = _records(_CASE_C)
        assert score_percent(records) == 72
        assert is_passed(records) is False

    def test_a_must_with_one_point_missing_fails(self) -> None:
        assert is_passed(_records(_CASE_A)) is False

    def test_everything_met_passes(self) -> None:
        assert is_passed(_records(_ALL_MET)) is True

    def test_a_list_without_must_passes_with_nothing_met(self) -> None:
        criteria = _in_force(_criterion("c1", "should"), _criterion("c2", "may"))
        answer = _answer(_not_met("c1"), _not_met("c2"))
        records = settle_verdicts(criteria, answer, None, work=_WORK, earlier={})
        assert score_percent(records) == 0
        assert is_passed(records) is True


class TestCriterionFromPoints:
    """Lock 3: a criterion with mandatory points ⇔ every point."""

    def test_every_point_met_meets_the_criterion(self) -> None:
        c5 = _settle(_CASE_A)["c5"]
        assert c5 == VerdictRecord(
            item_id="c5",
            item_kind=VerdictItemKind.CRITERION,
            weight=WeightCategory.SHOULD,
            verdict=MET,
            model_verdict=None,
        )

    def test_one_point_not_met_fails_the_criterion(self) -> None:
        settled = _settle(_CASE_B)
        assert [settled[i].verdict for i in ("c5.p1", "c5.p2", "c5.p3")] == [
            MET,
            NOT_MET,
            MET,
        ]
        assert settled["c5"].verdict is NOT_MET

    def test_one_point_on_a_quote_the_work_lacks_fails_the_criterion(self) -> None:
        answer = _answer(
            _met("c1", "def fibonacci(n):"),
            _met("c2.p1", "return fibonacci(n - 1)"),
            _met("c2.p2", _ABSENT),
            _not_met("c3"),
            _not_met("c4"),
            *(_not_met(item) for item in ("c5.p1", "c5.p2", "c5.p3")),
        )
        settled = _settle(answer)
        assert settled["c2.p2"].verdict is NOT_MET
        assert settled["c2"].verdict is NOT_MET

    def test_points_are_rows_of_their_own_without_weight(self) -> None:
        settled = _settle(_CASE_A)
        assert settled["c2.p1"].item_kind is VerdictItemKind.POINT
        assert settled["c2.p1"].weight is None
        assert settled["c2.p1"].model_verdict is MET

    def test_rows_follow_the_list_criterion_first_then_its_points(self) -> None:
        records = settle_verdicts(_LIST, _CASE_A, None, work=_WORK, earlier={})
        assert [r.item_id for r in records] == [
            "c1",
            "c2",
            "c2.p1",
            "c2.p2",
            "c3",
            "c4",
            "c5",
            "c5.p1",
            "c5.p2",
            "c5.p3",
        ]


class TestJudgedItems:
    def test_points_replace_their_criterion(self) -> None:
        assert judged_items(_LIST.criteria) == (
            "c1",
            "c2.p1",
            "c2.p2",
            "c3",
            "c4",
            "c5.p1",
            "c5.p2",
            "c5.p3",
        )

    def test_a_code_test_criterion_is_judged_by_a_model_verdict(self) -> None:
        criteria = (_criterion("c1", "must", method="code_test"),)
        assert judged_items(criteria) == ("c1",)


class TestQuoteNotInTheWork:
    """Lock 4, the pure part: a quote the work lacks gives no "met"."""

    def test_met_on_an_absent_quote_reads_not_met_flagged(self) -> None:
        answer = _answer(
            _met("c1", "def fibonacci(n):"),
            _met("c2.p1", "return fibonacci(n - 1)"),
            _met("c2.p2", "fibonacci(n - 2)"),
            _met("c3", _ABSENT),
            _not_met("c4"),
            *(_not_met(item) for item in ("c5.p1", "c5.p2", "c5.p3")),
        )
        assert _settle(answer)["c3"] == VerdictRecord(
            item_id="c3",
            item_kind=VerdictItemKind.CRITERION,
            weight=WeightCategory.SHOULD,
            verdict=NOT_MET,
            model_verdict=MET,
            quote=_ABSENT,
            quote_not_found=True,
        )

    def test_the_mets_the_work_lacks_are_asked_again(self) -> None:
        answer = _answer(
            _met("c1", "def fibonacci(n):"),
            _met("c2.p1", _ABSENT),
            _not_met("c2.p2"),
            _met("c3", "return n"),  # 7 characters: too short to count
            _met("c4", "fibonacci(n - 2)"),
        )
        assert items_to_ask_again(answer, _WORK) == (
            RepeatItem("c2.p1", RepeatReason.QUOTE_NOT_FOUND),
            RepeatItem("c3", RepeatReason.QUOTE_TOO_SHORT),
        )

    @pytest.mark.parametrize(
        "quote",
        [
            "return   fibonacci( n-1 )",
            "returnfibonacci(n-1)",
            "\treturn fibonacci(n - 1)",
            f"return{_NBSP}fibonacci(n - 1)",
            f"{_BOM}return fibonacci(n - 1)",
        ],
        ids=["spaces", "no-spaces", "tab", "nbsp", "bom"],
    )
    def test_whitespace_and_the_bom_do_not_hide_a_quote(self, quote: str) -> None:
        assert _WORK.find(quote) == QuotePlace("fibonacci.py", (4, 4))

    def test_a_quote_over_a_line_break_is_found(self) -> None:
        assert _WORK.find("if n < 2: return n") == QuotePlace("fibonacci.py", (2, 3))

    def test_crlf_work_is_read_as_lf(self) -> None:
        work = SubmittedWork(
            [WorkFile("fibonacci.py", _SOLUTION.replace("\n", "\r\n"), True)]
        )
        assert work.find("assert fibonacci(10) == 55") == QuotePlace(
            "fibonacci.py", (8, 8)
        )

    def test_a_bom_at_the_start_of_the_work_is_not_seen(self) -> None:
        work = SubmittedWork([WorkFile("fibonacci.py", _BOM + _SOLUTION, True)])
        assert work.find("def fibonacci(n):") == QuotePlace("fibonacci.py", (1, 1))

    def test_a_decomposed_quote_finds_a_composed_work(self) -> None:
        text = "Рекурсія зупиняється на базовому випадку й повертає n.\n"
        work = SubmittedWork([WorkFile("answer.md", text, True)])
        assert text == unicodedata.normalize("NFC", text)
        quote = unicodedata.normalize("NFD", "випадку й повертає")
        assert quote != "випадку й повертає"
        assert work.find(quote) == QuotePlace("answer.md", (1, 1))

    def test_a_composed_quote_finds_a_decomposed_work(self) -> None:
        text = unicodedata.normalize("NFD", "Café: the base case returns n\n")
        work = SubmittedWork([WorkFile("answer.md", text, True)])
        assert text != unicodedata.normalize("NFC", text)
        assert unicodedata.normalize("NFC", "Café") == "Café"
        assert work.find("Café: the base") == QuotePlace("answer.md", (1, 1))

    def test_a_quote_shorter_than_the_minimum_is_not_found(self) -> None:
        # "return n" is on line 3, but it is seven non-whitespace characters.
        assert len("returnn") == MIN_QUOTE_CHARS - 1
        assert _WORK.find("return n") is None

    def test_a_quote_of_the_minimum_length_is_found(self) -> None:
        work = SubmittedWork([WorkFile("a.txt", "x = 1\nab cd ef gh\n", True)])
        assert len("abcdefgh") == MIN_QUOTE_CHARS
        assert work.find("ab cd ef gh") == QuotePlace("a.txt", (2, 2))

    def test_a_quote_never_spans_two_files(self) -> None:
        work = SubmittedWork(
            [
                WorkFile("first.py", "print('first file')\n", True),
                WorkFile("second.py", "print('second file')\n", True),
            ]
        )
        assert work.find("print('first file')") is not None
        assert work.find("file')print('second") is None

    def test_a_file_name_is_not_text_of_the_work(self) -> None:
        work = SubmittedWork(
            [WorkFile("solutions/fibonacci_recursive.py", _SOLUTION, True)]
        )
        assert work.find("fibonacci_recursive.py") is None


class TestQuotePlace:
    def test_one_line(self) -> None:
        assert _WORK.find("def test_fibonacci():") == QuotePlace("fibonacci.py", (7, 7))

    def test_the_first_place_wins(self) -> None:
        # "fibonacci(n" stands on lines 1 and 4.
        assert _WORK.find("fibonacci(n") == QuotePlace("fibonacci.py", (1, 1))

    def test_lines_count_inside_the_file_of_an_archive(self) -> None:
        work = SubmittedWork(
            [
                WorkFile("README.md", "# Fibonacci\n\nRun the tests.\n", True),
                WorkFile("src/fibonacci.py", _SOLUTION, True),
            ]
        )
        assert work.find("assert fibonacci(10) == 55") == QuotePlace(
            "src/fibonacci.py", (8, 8)
        )

    def test_blank_lines_count(self) -> None:
        work = SubmittedWork([WorkFile("a.py", "\n\n\nprint('hello world')\n", True)])
        assert work.find("print('hello world')") == QuotePlace("a.py", (4, 4))

    def test_a_document_is_placed_by_its_file_alone(self) -> None:
        work = SubmittedWork([WorkFile("essay.docx", _SOLUTION, has_lines=False)])
        assert work.find("assert fibonacci(10) == 55") == QuotePlace("essay.docx", None)

    def test_a_met_record_carries_its_place(self) -> None:
        c3 = _settle(_CASE_A)["c3"]
        assert (c3.quote, c3.quote_file, c3.quote_lines) == (
            "assert fibonacci(10) == 55",
            "fibonacci.py",
            (8, 8),
        )


class TestRepeat:
    """The repeated request's word replaces the first one, for its items only."""

    _FIRST = _answer(
        _met("c1", "def fibonacci(n):"),
        _met("c2.p1", _ABSENT),
        _met("c2.p2", "return n"),
        _not_met("c3"),
        _met("c4", "fibonacci(n - 2)"),
        *(_not_met(item) for item in ("c5.p1", "c5.p2", "c5.p3")),
    )

    def test_found_on_the_repeat_is_met_and_marked_retried(self) -> None:
        repeat = read_answer(
            _content(
                _met("c2.p1", "return fibonacci(n - 1)"),
                _met("c2.p2", "if n < 2: return n"),
            ),
            [item.id for item in items_to_ask_again(self._FIRST, _WORK)],
        )
        settled = _settle(self._FIRST, repeat)
        assert settled["c2.p1"].verdict is MET
        assert settled["c2.p1"].retried is True
        assert settled["c2.p1"].quote_lines == (4, 4)
        assert settled["c2.p2"].quote_lines == (2, 3)
        assert settled["c2"].verdict is MET
        assert settled["c1"].retried is False
        assert settled["c4"].retried is False

    def test_still_unfound_after_the_repeat_is_not_met_flagged(self) -> None:
        repeat = _answer(_met("c2.p1", _ABSENT), _met("c2.p2", "return n"))
        settled = _settle(self._FIRST, repeat)
        for item in ("c2.p1", "c2.p2"):
            assert settled[item].verdict is NOT_MET
            assert settled[item].model_verdict is MET
            assert settled[item].retried is True
            assert settled[item].quote_not_found is True
        assert settled["c2"].verdict is NOT_MET

    def test_a_repeat_that_says_not_met_is_not_flagged(self) -> None:
        repeat = _answer(_not_met("c2.p1"), _not_met("c2.p2"))
        settled = _settle(self._FIRST, repeat)
        assert settled["c2.p1"].verdict is NOT_MET
        assert settled["c2.p1"].model_verdict is NOT_MET
        assert settled["c2.p1"].retried is True
        assert settled["c2.p1"].quote_not_found is False

    def test_answers_that_miss_an_item_are_refused(self) -> None:
        with pytest.raises(ValueError, match="do not cover exactly"):
            _settle(_answer(_met("c1", "def fibonacci(n):")))

    def test_a_repeat_about_an_item_not_in_the_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="do not cover exactly"):
            _settle(self._FIRST, _answer(_not_met("c9")))


_ONE = _in_force(_criterion("c1", "must"), _criterion("c3", "should"))


def _one(verdict: dict[str, Any]) -> EvaluationAnswer:
    """An answer on :data:`_ONE`: ``verdict`` on c1, a standing "met" on c3."""
    return _answer(verdict, _met("c3", "assert fibonacci(10) == 55"))


class TestDefectOfOneVerdict:
    """A defect of one verdict: that verdict alone is asked again, with its reason.

    Ratified 2026-10-02: only a defect of the answer as a whole repeats the
    whole answer (:class:`TestReadAnswer`); the field a verdict does not use is
    ignored.
    """

    @pytest.mark.parametrize(
        ("verdict", "reason"),
        [
            (
                {"id": "c1", "verdict": "met", "quote": None, "missing": None},
                RepeatReason.NO_QUOTE,
            ),
            (_met("c1", "   "), RepeatReason.NO_QUOTE),
            (
                _met("c1", "def fibonacci(n):\n    if n < 2:"),
                RepeatReason.QUOTE_NOT_ONE_LINE,
            ),
            (_met("c1", "x" * (MAX_QUOTE_CHARS + 1)), RepeatReason.QUOTE_TOO_LONG),
            (_met("c1", "return n"), RepeatReason.QUOTE_TOO_SHORT),
            (_met("c1", _ABSENT), RepeatReason.QUOTE_NOT_FOUND),
            (
                {"id": "c1", "verdict": "not_met", "quote": None, "missing": None},
                RepeatReason.NO_SENTENCE,
            ),
            (
                {"id": "c1", "verdict": "not_met", "quote": None, "missing": " "},
                RepeatReason.NO_SENTENCE,
            ),
        ],
        ids=[
            "no-quote",
            "blank-quote",
            "two-lines",
            "too-long",
            "too-short",
            "not-found",
            "no-sentence",
            "blank-sentence",
        ],
    )
    def test_the_defective_verdict_alone_is_asked_again(
        self, verdict: dict[str, Any], reason: RepeatReason
    ) -> None:
        assert items_to_ask_again(_one(verdict), _WORK) == (RepeatItem("c1", reason),)

    def test_a_two_line_quote_in_the_work_is_still_asked_again(self) -> None:
        quote = "def fibonacci(n):\n    if n < 2:"
        assert _WORK.find(quote) == QuotePlace("fibonacci.py", (1, 2))
        assert items_to_ask_again(_one(_met("c1", quote)), _WORK) == (
            RepeatItem("c1", RepeatReason.QUOTE_NOT_ONE_LINE),
        )

    def test_a_quote_over_the_maximum_in_the_work_is_still_asked_again(self) -> None:
        line = "a" * (MAX_QUOTE_CHARS + 1)
        work = SubmittedWork([WorkFile("long.txt", line + "\n", True)])
        assert work.find(line) is not None
        answer = _answer(_met("c1", line), _not_met("c3"))
        assert items_to_ask_again(answer, work) == (
            RepeatItem("c1", RepeatReason.QUOTE_TOO_LONG),
        )

    def test_a_quote_of_the_maximum_length_stands(self) -> None:
        line = "a" * MAX_QUOTE_CHARS
        work = SubmittedWork([WorkFile("long.txt", line + "\n", True)])
        answer = _answer(_met("c1", line), _not_met("c3"))
        assert items_to_ask_again(answer, work) == ()
        records = settle_verdicts(_ONE, answer, None, work=work, earlier={})
        assert records[0].verdict is MET

    def test_a_trailing_newline_does_not_make_two_lines(self) -> None:
        answer = _one(_met("c1", "def fibonacci(n):\n"))
        assert items_to_ask_again(answer, _WORK) == ()
        c1 = settle_verdicts(_ONE, answer, None, work=_WORK, earlier={})[0]
        assert (c1.verdict, c1.quote) == (MET, "def fibonacci(n):")

    def test_the_sentence_of_a_met_is_ignored(self) -> None:
        answer = _one(
            {
                "id": "c1",
                "verdict": "met",
                "quote": "def fibonacci(n):",
                "missing": "And this is missing.",
            }
        )
        assert items_to_ask_again(answer, _WORK) == ()
        c1 = settle_verdicts(_ONE, answer, None, work=_WORK, earlier={})[0]
        assert (c1.verdict, c1.quote, c1.missing) == (MET, "def fibonacci(n):", None)

    def test_the_sentence_of_a_met_that_falls_is_not_stored(self) -> None:
        # A "met" whose quote does not stand reads "not met"; the sentence the
        # model put beside its "met" is still not the verdict's, so the row has
        # none.
        talkative = {
            "id": "c1",
            "verdict": "met",
            "quote": _ABSENT,
            "missing": "And this is missing.",
        }
        c1 = _settle(_one(talkative), _answer(talkative), criteria=_ONE)["c1"]
        assert (c1.verdict, c1.quote_not_found) == (NOT_MET, True)
        assert c1.missing is None

    def test_the_quote_of_a_not_met_is_ignored_and_not_stored(self) -> None:
        answer = _one(
            {
                "id": "c1",
                "verdict": "not_met",
                "quote": "def fibonacci(n):",
                "missing": "No base case.",
            }
        )
        assert items_to_ask_again(answer, _WORK) == ()
        c1 = settle_verdicts(_ONE, answer, None, work=_WORK, earlier={})[0]
        assert c1 == VerdictRecord(
            item_id="c1",
            item_kind=VerdictItemKind.CRITERION,
            weight=WeightCategory.MUST,
            verdict=NOT_MET,
            model_verdict=NOT_MET,
            missing="No base case.",
        )

    @pytest.mark.parametrize(
        ("quote", "stored"),
        [
            (None, None),
            ("def fibonacci(n):\n    if n < 2:", "def fibonacci(n):\n    if n < 2:"),
            ("x" * (MAX_QUOTE_CHARS + 1), "x" * (MAX_QUOTE_CHARS + 1)),
            (_ABSENT, _ABSENT),
        ],
        ids=["no-quote", "two-lines", "too-long", "not-found"],
    )
    def test_a_met_that_still_does_not_stand_reads_not_met_flagged(
        self, quote: str | None, stored: str | None
    ) -> None:
        defective = {"id": "c1", "verdict": "met", "quote": quote, "missing": None}
        repeat = _answer(defective)
        c1 = _settle(_one(defective), repeat, criteria=_ONE)["c1"]
        assert c1 == VerdictRecord(
            item_id="c1",
            item_kind=VerdictItemKind.CRITERION,
            weight=WeightCategory.MUST,
            verdict=NOT_MET,
            model_verdict=MET,
            quote=stored,
            retried=True,
            quote_not_found=True,
        )

    def test_a_not_met_still_without_a_sentence_reads_not_met_without_one(
        self,
    ) -> None:
        silent = {"id": "c1", "verdict": "not_met", "quote": None, "missing": None}
        c1 = _settle(_one(silent), _answer(silent), criteria=_ONE)["c1"]
        assert c1 == VerdictRecord(
            item_id="c1",
            item_kind=VerdictItemKind.CRITERION,
            weight=WeightCategory.MUST,
            verdict=NOT_MET,
            model_verdict=NOT_MET,
            retried=True,
        )

    def test_the_safeguard_keeps_a_met_whose_quote_did_not_stand(self) -> None:
        defective = _met("c1", "def fibonacci(n):\n    if n < 2:")
        earlier = {"c1": _Earlier(quote="def fibonacci(n):")}
        c1 = _settle(
            _one(defective), _answer(defective), criteria=_ONE, earlier=earlier
        )["c1"]
        assert (c1.verdict, c1.model_verdict, c1.quote) == (
            MET,
            MET,
            "def fibonacci(n):",
        )
        assert c1.quote_not_found is True
        assert c1.safeguard_submission_id == _EARLIER_SUBMISSION

    def test_the_safeguard_keeps_a_not_met_without_a_sentence(self) -> None:
        silent = {"id": "c1", "verdict": "not_met", "quote": None, "missing": None}
        earlier = {"c1": _Earlier(quote="def fibonacci(n):")}
        c1 = _settle(_one(silent), _answer(silent), criteria=_ONE, earlier=earlier)[
            "c1"
        ]
        assert (c1.verdict, c1.missing) == (MET, None)
        assert c1.safeguard_submission_id == _EARLIER_SUBMISSION

    def test_each_reason_is_put_in_words(self) -> None:
        words = {reason: RepeatItem("c1", reason).feedback for reason in RepeatReason}
        assert all(text.strip() for text in words.values())
        assert len(set(words.values())) == len(RepeatReason)
        assert str(MAX_QUOTE_CHARS) in words[RepeatReason.QUOTE_TOO_LONG]
        assert str(MIN_QUOTE_CHARS) in words[RepeatReason.QUOTE_TOO_SHORT]


class TestReadAnswer:
    """The answer as a whole: what makes the whole answer be asked again."""

    _EXPECTED = ("c1", "c2.p1")

    def _refusal(self, content: str) -> str:
        with pytest.raises(StructuralRetryError) as caught:
            read_answer(content, self._EXPECTED)
        return caught.value.feedback

    def test_a_well_formed_answer_is_read(self) -> None:
        answer = read_answer(
            _content(_met("c1", "def fibonacci(n):"), _not_met("c2.p1")),
            self._EXPECTED,
        )
        assert [(v.id, v.verdict) for v in answer.verdicts] == [
            ("c1", MET),
            ("c2.p1", NOT_MET),
        ]

    def test_missing_unexpected_and_repeated_items_are_named(self) -> None:
        feedback = self._refusal(
            _content(_not_met("c1"), _not_met("c1"), _not_met("c7"))
        )
        assert "missing ['c2.p1']" in feedback
        assert "unexpected ['c7']" in feedback
        assert "repeated ['c1']" in feedback

    def test_an_invented_identifier_is_refused(self) -> None:
        feedback = self._refusal(
            _content(_not_met("c1"), _not_met("c2.p1"), _not_met("c2.p2"))
        )
        assert "unexpected ['c2.p2']" in feedback

    @pytest.mark.parametrize(
        "verdict",
        [
            {"id": "c1", "verdict": "met", "quote": None, "missing": None},
            _met("c1", "def fibonacci(n):\n    if n < 2:"),
            _met("c1", "x" * (MAX_QUOTE_CHARS + 1)),
            _met("c1", "return n"),
            _met("c1", _ABSENT),
            {"id": "c1", "verdict": "not_met", "quote": None, "missing": None},
            {"id": "c1", "verdict": "not_met", "quote": None, "missing": " "},
            {
                "id": "c1",
                "verdict": "met",
                "quote": "def fibonacci(n):",
                "missing": "And this is missing.",
            },
            {
                "id": "c1",
                "verdict": "not_met",
                "quote": "def fibonacci(n):",
                "missing": "No base case.",
            },
        ],
        ids=[
            "met-no-quote",
            "two-lines",
            "too-long",
            "too-short",
            "not-found",
            "not-met-no-sentence",
            "not-met-blank-sentence",
            "met-with-sentence",
            "not-met-with-quote",
        ],
    )
    def test_a_defect_of_one_verdict_does_not_refuse_the_answer(
        self, verdict: dict[str, Any]
    ) -> None:
        answer = read_answer(_content(verdict, _not_met("c2.p1")), self._EXPECTED)
        assert [v.id for v in answer.verdicts] == ["c1", "c2.p1"]

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            '{"verdicts": [{"id": "c1", "verdict": "partly", "quote": null, '
            '"missing": null}]}',
            '{"verdicts": [{"id": "c1", "verdict": "met", "quote": "def f(n):"}]}',
            '{"verdicts": [], "score": 100}',
        ],
        ids=["not-json", "third-state", "field-left-out", "extra-key"],
    )
    def test_an_answer_off_the_schema_is_refused(self, content: str) -> None:
        feedback = self._refusal(content)
        assert feedback.endswith("valid JSON matching the schema.")


class TestAnswerSchema:
    def test_every_field_is_required_and_nothing_else_is_allowed(self) -> None:
        # The form a strict schema on the wire (task 09a) asks for.
        schema = EvaluationAnswer.model_json_schema()
        defs = schema["$defs"].values()
        objects = [schema, *(d for d in defs if d.get("type") == "object")]
        assert len(objects) == 2
        for obj in objects:
            assert obj["additionalProperties"] is False
            assert set(obj["required"]) == set(obj["properties"])


@dataclass(frozen=True)
class _Earlier:
    """An earlier verdict, as ``previous_verdicts`` returns its rows."""

    submission_id: uuid.UUID = _EARLIER_SUBMISSION
    criteria_layer: str = "model"
    criteria_source_id: uuid.UUID = _LIST_ID
    verdict: str = "met"
    quote: str | None = "return fibonacci(n - 1)"


class TestSafeguard:
    """Lock 5, the pure part: an earlier "met" does not get worse."""

    _NOW = _answer(
        _met("c1", "def fibonacci(n):"),
        _not_met("c2.p1", "The recursive step is not there."),
        _met("c2.p2", "fibonacci(n - 2)"),
        _not_met("c3"),
        _met("c4", _ABSENT),
        *(_not_met(item) for item in ("c5.p1", "c5.p2", "c5.p3")),
    )

    def test_a_not_met_with_the_earlier_quote_in_the_work_stays_met(self) -> None:
        settled = _settle(self._NOW, earlier={"c2.p1": _Earlier()})
        assert settled["c2.p1"] == VerdictRecord(
            item_id="c2.p1",
            item_kind=VerdictItemKind.POINT,
            weight=None,
            verdict=MET,
            model_verdict=NOT_MET,
            quote="return fibonacci(n - 1)",
            quote_file="fibonacci.py",
            quote_lines=(4, 4),
            missing="The recursive step is not there.",
            safeguard_submission_id=_EARLIER_SUBMISSION,
        )

    def test_a_kept_point_meets_its_criterion(self) -> None:
        settled = _settle(self._NOW, earlier={"c2.p1": _Earlier()})
        assert settled["c2"].verdict is MET

    def test_a_met_on_an_unfound_quote_is_kept_and_stays_flagged(self) -> None:
        earlier = {"c4": _Earlier(quote="assert fibonacci(10) == 55")}
        c4 = _settle(self._NOW, earlier=earlier)["c4"]
        assert (c4.verdict, c4.model_verdict) == (MET, MET)
        assert c4.quote == "assert fibonacci(10) == 55"
        assert c4.quote_lines == (8, 8)
        assert c4.quote_not_found is True
        assert c4.safeguard_submission_id == _EARLIER_SUBMISSION

    def test_the_earlier_quote_is_matched_as_any_quote(self) -> None:
        earlier = {"c2.p1": _Earlier(quote="return fibonacci( n - 1 )")}
        assert _settle(self._NOW, earlier=earlier)["c2.p1"].verdict is MET

    def test_another_list_of_the_same_layer_does_not_count(self) -> None:
        earlier = {"c2.p1": _Earlier(criteria_source_id=_OTHER_LIST_ID)}
        c2p1 = _settle(self._NOW, earlier=earlier)["c2.p1"]
        assert c2p1.verdict is NOT_MET
        assert c2p1.safeguard_submission_id is None

    def test_another_layer_with_the_same_id_does_not_count(self) -> None:
        earlier = {"c2.p1": _Earlier(criteria_layer="author")}
        c2p1 = _settle(self._NOW, earlier=earlier)["c2.p1"]
        assert c2p1.verdict is NOT_MET
        assert c2p1.safeguard_submission_id is None

    def test_an_earlier_not_met_keeps_nothing(self) -> None:
        earlier = {"c2.p1": _Earlier(verdict="not_met", quote=None)}
        assert _settle(self._NOW, earlier=earlier)["c2.p1"].verdict is NOT_MET

    def test_an_earlier_not_met_on_an_unfound_quote_keeps_nothing(self) -> None:
        # The earlier row a "met" on a quote that was not found leaves: it
        # says not_met and still holds the quote. That quote being in THIS
        # work does not make the earlier verdict a "met" to keep.
        earlier = {"c2.p1": _Earlier(verdict="not_met")}
        assert earlier["c2.p1"].quote == "return fibonacci(n - 1)"
        assert _WORK.find("return fibonacci(n - 1)") is not None
        c2p1 = _settle(self._NOW, earlier=earlier)["c2.p1"]
        assert c2p1.verdict is NOT_MET
        assert c2p1.safeguard_submission_id is None

    def test_an_earlier_quote_gone_from_the_work_keeps_nothing(self) -> None:
        earlier = {"c2.p1": _Earlier(quote=_ABSENT)}
        c2p1 = _settle(self._NOW, earlier=earlier)["c2.p1"]
        assert c2p1.verdict is NOT_MET
        assert c2p1.safeguard_submission_id is None

    def test_an_earlier_verdict_on_another_item_keeps_nothing(self) -> None:
        settled = _settle(self._NOW, earlier={"c3": _Earlier()})
        assert settled["c3"].safeguard_submission_id == _EARLIER_SUBMISSION
        assert settled["c2.p1"].verdict is NOT_MET
        assert settled["c2.p1"].safeguard_submission_id is None

    def test_a_met_that_stands_on_its_own_does_not_fire_it(self) -> None:
        earlier = {"c1": _Earlier(quote="assert fibonacci(10) == 55")}
        c1 = _settle(self._NOW, earlier=earlier)["c1"]
        assert c1.verdict is MET
        assert c1.quote == "def fibonacci(n):"
        assert c1.safeguard_submission_id is None

    def test_the_author_layer_is_matched_too(self) -> None:
        criteria = _in_force(*_LIST.criteria, layer=CriteriaLayer.AUTHOR)
        earlier = {"c2.p1": _Earlier(criteria_layer="author")}
        settled = _settle(self._NOW, criteria=criteria, earlier=earlier)
        assert settled["c2.p1"].safeguard_submission_id == _EARLIER_SUBMISSION


class TestSameInputSameResult:
    """Lock 9: the same machine input gives the same rows and the same score."""

    def test_settling_twice_gives_the_same_rows(self) -> None:
        earlier = {"c2.p1": _Earlier()}
        first = settle_verdicts(_LIST, _CASE_A, None, work=_WORK, earlier=earlier)
        work = SubmittedWork([WorkFile("fibonacci.py", _SOLUTION, True)])
        second = settle_verdicts(_LIST, _CASE_A, None, work=work, earlier=earlier)
        assert first == second
        assert (score_percent(first), is_passed(first)) == (
            score_percent(second),
            is_passed(second),
        )


def test_documentation_examples_run() -> None:
    results = doctest.testmod(criteria_verdicts, verbose=False)
    assert results.attempted > 0, "the module's documentation lost its examples"
    assert results.failed == 0
