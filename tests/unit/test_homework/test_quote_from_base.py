"""A quote of the author's code is not the student's (project chain K3).

Pure rules, no session and no model. A project is the author's base with the
student's work on top; a quote that the work's own search finds in any text
file of the base -- without whitespace, in NFC, a piece of a line for a quote
of eight characters or more, a whole line for a shorter one -- is the
author's, however it reached the student's files: unchanged lines of a changed file, a
file moved or renamed, a file the student never touched. It is asked about
once more (``RepeatReason.QUOTE_FROM_BASE``); a "met" that still stands on
the base after the repeat, or that the repeat took back, is "not met" with
the author's flag ``quote_from_base`` -- never unconfirmed.

Locks of the K3 brief held here: a (a new file of the student's), b (unchanged
lines of a changed file; the repeat's three outcomes), c (a renamed file).
The door's part -- the corpus it reads -- is
``tests/unit/test_project_base_corpus.py``; the row on a real database is
``tests/integration/test_criteria_evaluation_stage_db.py``.
"""

from __future__ import annotations

import json
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
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_list_service import CriteriaInForce
from course_supporter.homework.criteria_verdicts import (
    EvaluationAnswer,
    RepeatItem,
    RepeatReason,
    SubmittedWork,
    WorkFile,
    is_passed,
    items_to_ask_again,
    read_answer,
    score_percent,
    settle_verdicts,
)
from course_supporter.storage.submission_verdict_repository import VerdictRecord

MET = VerdictValue.MET
NOT_MET = VerdictValue.NOT_MET

_LIST_ID = uuid.UUID("01999999-0000-7000-8000-0000000000b1")

# The author's base: a loader the task builds on.
_BASE_APP = (
    "def load(path):\n"  # 1
    "    with open(path) as handle:\n"  # 2
    "        return handle.read()\n"  # 3
)
_BASE = (
    WorkFile("app.py", _BASE_APP, True),
    WorkFile("README.md", "# Word count\n\nRun `python app.py`.\n", True),
)

# The student's work: app.py changed -- the loader kept, a counter added --
# and a new file of their own.
_APP = (
    _BASE_APP  # lines 1-3, the author's
    + "\n"  # 4
    + "def count_words(text):\n"  # 5
    + "    return len(text.split())\n"  # 6
)
_STATS = "def mean(values):\n    return sum(values) / len(values)\n"
_FILES = (WorkFile("app.py", _APP, True), WorkFile("lib/stats.py", _STATS, True))
_WORK = SubmittedWork(_FILES, base=_BASE)

_AUTHORS = "with open(path) as handle:"  # line 2 of app.py: the base's
_STUDENTS = "return len(text.split())"  # line 6 of app.py: the student's
_NEW_FILE = "return sum(values) / len(values)"  # lib/stats.py, line 2
_README = "Run `python app.py`."  # the base's README, not in the work at all
_MISSING = "The work does not count words."

_FEEDBACK = (
    "the quote is taken from the code the author gave; a quote from the "
    "student's own code is needed"
)


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


# c1 "must" (3), c2 "should" (2): 5 in all.
_LIST = CriteriaInForce(
    criteria=(_criterion("c1", "must"), _criterion("c2", "should")),
    layer=CriteriaLayer.MODEL,
    source_id=_LIST_ID,
)


def _met(item: str, quote: str) -> dict[str, Any]:
    return {"id": item, "verdict": "met", "quote": quote, "missing": None}


def _not_met(item: str, missing: str = _MISSING) -> dict[str, Any]:
    return {"id": item, "verdict": "not_met", "quote": None, "missing": missing}


def _answer(*verdicts: dict[str, Any]) -> EvaluationAnswer:
    content = json.dumps({"verdicts": list(verdicts)})
    return read_answer(content, [v["id"] for v in verdicts])


def _settle(
    first: EvaluationAnswer,
    repeat: EvaluationAnswer | None = None,
    *,
    work: SubmittedWork = _WORK,
    criteria: CriteriaInForce = _LIST,
    earlier: dict[str, Any] | None = None,
) -> dict[str, VerdictRecord]:
    records = settle_verdicts(criteria, first, repeat, work=work, earlier=earlier or {})
    return {record.item_id: record for record in records}


class TestTheStudentsOwnCode:
    def test_a_a_quote_of_a_new_file_stands(self) -> None:
        """Lock a: the student's new file is theirs -- met, at its place."""
        first = _answer(_met("c1", _NEW_FILE), _met("c2", _STUDENTS))

        assert items_to_ask_again(first, _WORK) == ()
        rows = _settle(first)
        assert rows["c1"] == VerdictRecord(
            item_id="c1",
            item_kind=VerdictItemKind.CRITERION,
            weight=WeightCategory.MUST,
            verdict=MET,
            model_verdict=MET,
            quote=_NEW_FILE,
            quote_file="lib/stats.py",
            quote_lines=(2, 2),
        )
        # The student's own lines of a changed file stand as well.
        assert (rows["c2"].verdict, rows["c2"].quote_lines) == (MET, (6, 6))
        assert not any(row.quote_from_base for row in rows.values())
        assert score_percent(rows.values()) == 100


class TestUnchangedLinesOfAChangedFile:
    """Lock b: the author's lines the student kept are the author's."""

    def test_b_it_is_asked_again_as_from_the_base(self) -> None:
        first = _answer(_met("c1", _AUTHORS), _met("c2", _STUDENTS))

        (again,) = items_to_ask_again(first, _WORK)
        assert again == RepeatItem("c1", RepeatReason.QUOTE_FROM_BASE)
        assert again.feedback == _FEEDBACK

    def test_b_a_repeat_on_the_students_code_is_met(self) -> None:
        first = _answer(_met("c1", _AUTHORS), _met("c2", _STUDENTS))
        repeat = _answer(_met("c1", _NEW_FILE))

        row = _settle(first, repeat)["c1"]

        assert (row.verdict, row.quote, row.quote_file) == (
            MET,
            _NEW_FILE,
            "lib/stats.py",
        )
        assert (row.retried, row.quote_from_base, row.quote_not_found) == (
            True,
            False,
            False,
        )

    def test_b_a_repeat_on_the_base_again_is_not_met_and_flagged(self) -> None:
        first = _answer(_met("c1", _AUTHORS), _met("c2", _STUDENTS))
        repeat = _answer(_met("c1", "return handle.read()"))

        rows = _settle(first, repeat)

        assert rows["c1"] == VerdictRecord(
            item_id="c1",
            item_kind=VerdictItemKind.CRITERION,
            weight=WeightCategory.MUST,
            verdict=NOT_MET,
            model_verdict=MET,
            quote="return handle.read()",
            retried=True,
            quote_from_base=True,
        )
        # Not unconfirmed: the "must" fails the work and counts for nothing.
        assert is_passed(rows.values(), layer=CriteriaLayer.MODEL) is False
        # 2 (c2) of 5 -> 40.
        assert score_percent(rows.values()) == 40

    def test_b_a_repeat_that_takes_it_back_is_not_met_and_flagged(self) -> None:
        first = _answer(_met("c1", _AUTHORS), _met("c2", _STUDENTS))
        repeat = _answer(_not_met("c1"))

        row = _settle(first, repeat)["c1"]

        assert row == VerdictRecord(
            item_id="c1",
            item_kind=VerdictItemKind.CRITERION,
            weight=WeightCategory.MUST,
            verdict=NOT_MET,
            model_verdict=NOT_MET,
            missing=_MISSING,
            retried=True,
            quote_from_base=True,
        )

    def test_b_the_comparison_is_the_quotes_own(self) -> None:
        """Indentation, CRLF and a decomposed accent do not hide the base."""
        base = "x = 1\r\n# Café: the author's note\r\n"
        work = SubmittedWork(
            [WorkFile("a.py", "x = 2\n#   Café:  the author's note\n", True)],
            base=[WorkFile("a.py", base, True)],
        )
        first = _answer(_met("c1", "# Café: the author's note"), _not_met("c2"))

        assert items_to_ask_again(first, work) == (
            RepeatItem("c1", RepeatReason.QUOTE_FROM_BASE),
        )


class TestTheAuthorsCodeElsewhere:
    def test_c_a_renamed_file_is_the_authors(self) -> None:
        """Lock c: base a.py, work b.py of the same text -- "new" to the delta."""
        work = SubmittedWork(
            [WorkFile("b.py", _BASE_APP, True)],
            base=[WorkFile("a.py", _BASE_APP, True)],
        )
        first = _answer(_met("c1", _AUTHORS), _not_met("c2"))

        assert work.find(_AUTHORS) is not None
        assert items_to_ask_again(first, work) == (
            RepeatItem("c1", RepeatReason.QUOTE_FROM_BASE),
        )

    def test_a_quote_of_a_file_the_student_did_not_touch_is_the_authors(
        self,
    ) -> None:
        """Not in the student's files, in the base's: the author's, not made up.

        The review's context shows unchanged files beside the changed ones; a
        quote of one is no more the student's than a kept line. After the
        repeat it is "not met" with the flag -- not unconfirmed, which would
        count it as met.
        """
        first = _answer(_met("c1", _README), _met("c2", _STUDENTS))

        assert items_to_ask_again(first, _WORK) == (
            RepeatItem("c1", RepeatReason.QUOTE_FROM_BASE),
        )
        row = _settle(first, _answer(_met("c1", _README)))["c1"]
        assert (row.verdict, row.quote_from_base, row.quote_not_found) == (
            NOT_MET,
            True,
            False,
        )


# A short quote: a whole line of the student's .gitignore (answer 5 of the
# Mentor session on #649 -- the base is searched as the work is).
_VENV = ".venv/"
_GITIGNORE = WorkFile(".gitignore", "__pycache__/\n.venv/\n", True)


class TestTheSameSearchAsInTheWork:
    """A quote is the base's when the work's own search finds it there."""

    def _first(self, *base: WorkFile) -> tuple[EvaluationAnswer, SubmittedWork]:
        work = SubmittedWork((*_FILES, _GITIGNORE), base=base)
        return _answer(_met("c1", _VENV), _met("c2", _STUDENTS)), work

    def test_a_short_whole_line_only_a_piece_of_a_line_of_the_base_stands(
        self,
    ) -> None:
        readme = WorkFile("README.md", "Create .venv/ with `uv venv`.\n", True)
        first, work = self._first(*_BASE, readme)

        assert work.is_from_base(_VENV) is False
        assert items_to_ask_again(first, work) == ()
        row = _settle(first, work=work)["c1"]
        assert (row.verdict, row.quote_file, row.quote_lines) == (
            MET,
            ".gitignore",
            (2, 2),
        )
        assert row.quote_from_base is False

    def test_the_same_whole_line_in_the_base_is_the_bases(self) -> None:
        base_gitignore = WorkFile(".gitignore", "# the author's\n  .venv/\n", True)
        first, work = self._first(*_BASE, base_gitignore)

        assert items_to_ask_again(first, work) == (
            RepeatItem("c1", RepeatReason.QUOTE_FROM_BASE),
        )

    def test_a_whole_line_of_a_base_document_is_no_line(self) -> None:
        """A document's lines are an artefact of extraction, as in the work."""
        notes = WorkFile("notes.docx", "Ignore\n.venv/\n", has_lines=False)
        first, work = self._first(*_BASE, notes)

        assert items_to_ask_again(first, work) == ()


class TestTheRestOfTheRules:
    @pytest.mark.parametrize(
        "second",
        [
            pytest.param("def count_lines(text):", id="made-up"),
            pytest.param(None, id="none"),
        ],
    )
    def test_after_the_base_a_repeat_without_the_students_code_is_flagged(
        self, second: str | None
    ) -> None:
        """Answer 1 (#649): the model never showed the student's code.

        Not unconfirmed, which would count the author's code as met.
        """
        first = _answer(_met("c1", _AUTHORS), _met("c2", _STUDENTS))
        repeat = _answer(
            {"id": "c1", "verdict": "met", "quote": second, "missing": None}
        )

        rows = _settle(first, repeat)

        assert (
            rows["c1"].verdict,
            rows["c1"].quote_not_found,
            rows["c1"].quote_from_base,
        ) == (NOT_MET, False, True)
        assert is_passed(rows.values(), layer=CriteriaLayer.MODEL) is False

    def test_after_the_base_the_students_code_badly_quoted_is_unconfirmed(
        self,
    ) -> None:
        """The student's own code in a quote of two lines: the ordinary rule."""
        first = _answer(_met("c1", _AUTHORS), _met("c2", _STUDENTS))
        repeat = _answer(_met("c1", "def count_words(text):\n" + _STUDENTS))

        row = _settle(first, repeat)["c1"]

        assert (row.verdict, row.quote_not_found, row.quote_from_base) == (
            NOT_MET,
            True,
            False,
        )

    def test_a_made_up_quote_without_the_base_is_unconfirmed_as_before(
        self,
    ) -> None:
        """No quote of the base first: the ordinary rule, untouched."""
        first = _answer(_met("c1", "def count_lines(text):"), _met("c2", _STUDENTS))
        repeat = _answer(_met("c1", "def count_lines(words):"))

        row = _settle(first, repeat)["c1"]

        assert (row.verdict, row.quote_not_found, row.quote_from_base) == (
            NOT_MET,
            True,
            False,
        )

    def test_a_quote_too_short_stays_too_short(self) -> None:
        """ "return" is in the base, but its fault is its length."""
        first = _answer(_met("c1", "return"), _met("c2", _STUDENTS))

        assert items_to_ask_again(first, _WORK) == (
            RepeatItem("c1", RepeatReason.QUOTE_TOO_SHORT),
        )

    def test_a_point_on_the_base_carries_the_flag_its_criterion_fails(
        self,
    ) -> None:
        criteria = CriteriaInForce(
            criteria=(_criterion("c1", "must", points=2),),
            layer=CriteriaLayer.MODEL,
            source_id=_LIST_ID,
        )
        first = _answer(_met("c1.p1", _AUTHORS), _met("c1.p2", _STUDENTS))
        repeat = _answer(_met("c1.p1", _AUTHORS))

        rows = _settle(first, repeat, criteria=criteria)

        assert (rows["c1.p1"].verdict, rows["c1.p1"].quote_from_base) == (
            NOT_MET,
            True,
        )
        # Answer 2 (#649): the criterion carries its point's flag.
        assert (
            rows["c1"].verdict,
            rows["c1"].quote_not_found,
            rows["c1"].quote_from_base,
        ) == (NOT_MET, False, True)
        assert is_passed(rows.values(), layer=CriteriaLayer.MODEL) is False

    def test_a_criterion_whose_points_stand_carries_no_flag(self) -> None:
        criteria = CriteriaInForce(
            criteria=(_criterion("c1", "must", points=2),),
            layer=CriteriaLayer.MODEL,
            source_id=_LIST_ID,
        )
        first = _answer(_met("c1.p1", _AUTHORS), _met("c1.p2", _STUDENTS))
        repeat = _answer(_met("c1.p1", _NEW_FILE))

        rows = _settle(first, repeat, criteria=criteria)

        assert (rows["c1"].verdict, rows["c1"].quote_from_base) == (MET, False)

    def test_the_safeguard_does_not_keep_a_quote_of_the_base(self) -> None:
        @dataclass(frozen=True)
        class _Earlier:
            submission_id: uuid.UUID
            criteria_layer: str
            criteria_source_id: uuid.UUID
            verdict: str
            quote: str | None

        earlier = {
            "c1": _Earlier(
                uuid.uuid4(), CriteriaLayer.MODEL.value, _LIST_ID, MET, _AUTHORS
            )
        }
        first = _answer(_not_met("c1"), _met("c2", _STUDENTS))

        row = _settle(first, earlier=earlier)["c1"]

        assert (row.verdict, row.safeguard_submission_id) == (NOT_MET, None)

    def test_without_a_base_nothing_changes(self) -> None:
        """Every type but a project: the same rows as a work without a base."""
        first = _answer(_met("c1", _AUTHORS), _met("c2", "def count_lines(t):"))
        repeat = _answer(_met("c2", _STUDENTS))
        plain = SubmittedWork(_FILES)

        assert items_to_ask_again(first, plain) == (
            RepeatItem("c2", RepeatReason.QUOTE_NOT_FOUND),
        )
        assert _settle(first, repeat, work=plain) == _settle(
            first, repeat, work=SubmittedWork(_FILES, base=())
        )
        assert _settle(first, repeat, work=plain)["c1"].verdict == MET
