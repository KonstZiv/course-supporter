"""The place of a problem in a project's code (project chain K5).

A project's "not met" may carry, in its unused ``quote``, the line of the
student's file where its problem stands. The code looks for it in the
student's files by the search a "met"'s quote meets and keeps the place
beside the verdict; the review then points the remark to it. Pinned here on
what the project's door really reads (``tests/_helpers/recorded_project.py``),
without a database or a model:

* lock a — a line of a NEW file is placed, and the remark points to
  ``path:N``; several lines to ``path:N-M``; a file without lines to ``path``;
* lock b — an author's line left in a CHANGED file is a place;
* lock c — a line the student's files do not have is no place, and asks
  nothing again;
* lock d — an unconfirmed "met" and an item fallen for the base's quote have
  no place;
* lock e — a criterion checked by points points to its first unmet point
  with a place, in the list's order, or nowhere;
* lock f — the resubmission safeguard never takes an earlier "not met" with a
  place for a "met";
* lock g — a text type's stage keeps no place: its rows and its review are
  what they were before K5;
* lock i — the quote rule of chain K3 still holds beside the places, and
  nothing switched a path, the type's server or the rules' version.

The same through the project's own stage, on recorded answers and a live
database: ``tests/integration/test_project_stages_db.py``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from tests._helpers.recorded_project import (
    AUTHORS_APP,
    QUOTE_OF_AUTHORS_LINE,
    QUOTE_OF_CHANGED,
    QUOTE_OF_NEIGHBOR,
    QUOTE_OF_NEW,
    STUDENTS_APP,
    read_recorded_project,
)

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
    EvaluationAnswer,
    RepeatReason,
    SubmittedWork,
    WorkFile,
    items_to_ask_again,
    read_answer,
    settle_verdicts,
)
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.text_result import (
    build_text_review,
    problem_positions,
)
from course_supporter.homework.verdict_explanation import (
    CriterionRemark,
    ExplanationAnswer,
)
from course_supporter.models.review_structure import Position
from course_supporter.storage.submission_verdict_repository import VerdictRecord

MET, NOT_MET = VerdictValue.MET, VerdictValue.NOT_MET
CRITERION, POINT = VerdictItemKind.CRITERION, VerdictItemKind.POINT

_LIST_ID = uuid.UUID("01999999-0000-7000-8000-0000000000a5")
_EARLIER_SUBMISSION = uuid.UUID("01999999-0000-7000-8000-0000000000e5")

# ``stats.py`` lines 2 and 3, the student's, given as one quote of two lines.
_TWO_LINES = "if not lines:\n        return 0.0"
# ``app.py`` line 6 of the base, which the student's version no longer has: a
# line a difference would show with ``-``.
_REMOVED = "print(len(lines))"
_MADE_UP = "return statistics.mean(map(len, lines))"
_MISSING = "Порожній файл не обробляється окремо."


def _criterion(cid: str, weight: str, points: int = 0) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Критерій {cid}",
            "evidence": f"Що показує {cid}",
            "weight": weight,
            "check_method": "mandatory_points" if points else "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [
                {"id": f"{cid}.p{n}", "text": f"Пункт {n} критерію {cid}"}
                for n in range(1, points + 1)
            ],
        }
    )


# c1 "must" on its own; c2 "should" by three points; c3 "may" on its own.
_LIST = CriteriaInForce(
    criteria=(
        _criterion("c1", "must"),
        _criterion("c2", "should", points=3),
        _criterion("c3", "may"),
    ),
    layer=CriteriaLayer.AUTHOR,
    source_id=_LIST_ID,
)


def _met(item: str, quote: str) -> dict[str, Any]:
    return {"id": item, "verdict": "met", "quote": quote, "missing": None}


def _not_met(item: str, quote: str | None = None) -> dict[str, Any]:
    return {"id": item, "verdict": "not_met", "quote": quote, "missing": _MISSING}


def _answer(*verdicts: dict[str, Any]) -> EvaluationAnswer:
    """An answer as the stage gets it: read and checked by :func:`read_answer`."""
    content = json.dumps({"verdicts": list(verdicts)}, ensure_ascii=False)
    return read_answer(content, [v["id"] for v in verdicts])


def _first(c1: dict[str, Any]) -> EvaluationAnswer:
    """The first answer, ``c1`` as given and every other item met."""
    return _answer(
        c1,
        _met("c2.p1", QUOTE_OF_CHANGED),
        _met("c2.p2", QUOTE_OF_CHANGED),
        _met("c2.p3", QUOTE_OF_NEW),
        _met("c3", QUOTE_OF_NEW),
    )


def _settle(
    answer: EvaluationAnswer,
    work: SubmittedWork,
    repeat: EvaluationAnswer | None = None,
    *,
    place_unmet: bool = True,
    earlier: dict[str, Any] | None = None,
) -> dict[str, VerdictRecord]:
    records = settle_verdicts(
        _LIST,
        answer,
        repeat,
        work=work,
        earlier=earlier or {},
        place_unmet=place_unmet,
    )
    return {record.item_id: record for record in records}


def _work_of(reading: DoorReading) -> SubmittedWork:
    """The work as the evaluation stage builds it from the project's door."""
    return SubmittedWork(reading.files, base=reading.base_files)


def _review(records: dict[str, VerdictRecord]) -> tuple[list[Any], str]:
    """The review of the rows, with a remark on every criterion not met."""
    rows = list(records.values())
    unmet = [
        r.item_id for r in rows if r.item_kind is CRITERION and r.verdict is NOT_MET
    ]
    explanation = ExplanationAnswer(
        passed=False,
        why="Причина вердикту за курсом.",
        remarks=tuple(
            CriterionRemark(
                id=cid, what=f"Як зроблено {cid}.", why="Чому.", todo="Як.", links=()
            )
            for cid in unmet
        ),
        mentor_voice=None,
    )
    structure, _ = build_text_review(
        rows=rows,
        layer=CriteriaLayer.AUTHOR,
        explanation=explanation,
        language="ukr",
    )
    return structure.new_remarks, assemble_review(structure)


@pytest.fixture()
async def reading(monkeypatch: pytest.MonkeyPatch) -> DoorReading:
    return await read_recorded_project(monkeypatch)


@pytest.fixture()
def work(reading: DoorReading) -> SubmittedWork:
    return _work_of(reading)


class TestThePlaceIsFound:
    """Lock a: the row keeps the line, the remark points to it."""

    def test_a_line_of_a_new_file(self, work: SubmittedWork) -> None:
        c1 = _settle(_first(_not_met("c1", QUOTE_OF_NEW)), work)["c1"]

        assert c1 == VerdictRecord(
            item_id="c1",
            item_kind=CRITERION,
            weight=WeightCategory.MUST,
            verdict=NOT_MET,
            model_verdict=NOT_MET,
            quote=QUOTE_OF_NEW,
            quote_file="stats.py",
            quote_lines=(4, 4),
            missing=_MISSING,
        )
        assert (c1.quote_line_start, c1.quote_line_end) == (4, 4)

    def test_the_remark_points_to_it_on_a_line_above_it(
        self, work: SubmittedWork
    ) -> None:
        records = _settle(_first(_not_met("c1", QUOTE_OF_NEW)), work)

        [remark] = _review(records)[0]
        markdown = _review(records)[1]

        assert remark.position == Position(kind="file", value="stats.py:4")
        assert "*файл stats.py:4*\n\n- **Що не так:** Як зроблено c1." in markdown

    def test_several_lines(self, work: SubmittedWork) -> None:
        records = _settle(_first(_not_met("c1", _TWO_LINES)), work)

        assert records["c1"].quote_lines == (2, 3)
        [remark] = _review(records)[0]
        assert remark.position == Position(kind="file", value="stats.py:2-3")
        assert "*файл stats.py:2-3*" in _review(records)[1]

    def test_a_file_without_lines_is_named_alone(self) -> None:
        document = SubmittedWork(
            [WorkFile("report.docx", "Висновок: дані не перевірено.", False)]
        )
        answer = _answer(
            _not_met("c1", "дані не перевірено"),
            *(_not_met(item) for item in ("c2.p1", "c2.p2", "c2.p3", "c3")),
        )

        records = _settle(answer, document)

        assert (records["c1"].quote_file, records["c1"].quote_lines) == (
            "report.docx",
            None,
        )
        assert problem_positions(records.values())["c1"] == Position(
            kind="file", value="report.docx"
        )
        assert "*файл report.docx*" in _review(records)[1]


class TestAnAuthorsLineInAChangedFile:
    """Lock b: the student's file, so the student's place to look."""

    def test_an_unchanged_line_of_the_author_is_a_place(
        self, reading: DoorReading, work: SubmittedWork
    ) -> None:
        app = {file.name: file.text for file in reading.files}["app.py"]
        assert QUOTE_OF_AUTHORS_LINE in app
        assert work.is_from_base(QUOTE_OF_AUTHORS_LINE)

        answer = _first(_not_met("c1", QUOTE_OF_AUTHORS_LINE))
        c1 = _settle(answer, work)["c1"]

        assert items_to_ask_again(answer, work) == ()
        assert (c1.quote, c1.quote_file, c1.quote_lines) == (
            QUOTE_OF_AUTHORS_LINE,
            "app.py",
            (7, 7),
        )
        assert (c1.quote_from_base, c1.quote_not_found) == (False, False)
        [remark] = _review({"c1": c1, **_others(work)})[0]
        assert remark.position == Position(kind="file", value="app.py:7")


def _others(work: SubmittedWork) -> dict[str, VerdictRecord]:
    """Every item but ``c1``, met."""
    records = _settle(_first(_not_met("c1")), work)
    return {cid: record for cid, record in records.items() if cid != "c1"}


class TestNoPlace:
    """Lock c: not in the student's files — no place, no repeat, no flag."""

    @pytest.mark.parametrize(
        "quote",
        [_MADE_UP, QUOTE_OF_NEIGHBOR, _REMOVED],
        ids=["made_up", "neighbor", "removed_line"],
    )
    def test_a_line_the_students_files_do_not_have(
        self, work: SubmittedWork, quote: str
    ) -> None:
        answer = _first(_not_met("c1", quote))

        to_repeat = items_to_ask_again(answer, work)
        c1 = _settle(answer, work)["c1"]

        assert to_repeat == ()
        assert c1 == VerdictRecord(
            item_id="c1",
            item_kind=CRITERION,
            weight=WeightCategory.MUST,
            verdict=NOT_MET,
            model_verdict=NOT_MET,
            missing=_MISSING,
        )
        [remark] = _review(_settle(answer, work))[0]
        assert remark.position is None

    def test_the_removed_line_is_the_bases_alone(self, work: SubmittedWork) -> None:
        """The case above is the one it names: the base's line, gone."""
        assert _REMOVED in AUTHORS_APP
        assert _REMOVED not in STUDENTS_APP.replace(" ", "")
        assert work.find(_REMOVED) is None


class TestNoPlaceForFailedEvidence:
    """Lock d: the quote of a failed "met" is evidence, never a place."""

    def test_an_unconfirmed_met(self, work: SubmittedWork) -> None:
        # Two lines of the student's file: in the work, and not a quote a
        # "met" may stand on — twice, so the "met" is unconfirmed.
        assert work.find(_TWO_LINES) is not None
        first = _first(_met("c1", _TWO_LINES))
        assert [i.reason for i in items_to_ask_again(first, work)] == [
            RepeatReason.QUOTE_NOT_ONE_LINE
        ]

        c1 = _settle(first, work, _answer(_met("c1", _TWO_LINES)))["c1"]

        assert (c1.verdict, c1.quote_not_found) == (NOT_MET, True)
        assert (c1.quote_file, c1.quote_lines) == (None, None)

    def test_an_item_fallen_for_the_bases_quote(self, work: SubmittedWork) -> None:
        # The first "met" stood on the author's code; the repeat took it back
        # and named a student's line, which IS in the work.
        first = _first(_met("c1", QUOTE_OF_NEIGHBOR))
        repeat = _answer(_not_met("c1", QUOTE_OF_NEW))

        c1 = _settle(first, work, repeat)["c1"]

        assert (c1.verdict, c1.quote_from_base) == (NOT_MET, True)
        assert (c1.quote, c1.quote_file, c1.quote_lines) == (None, None, None)

    def test_a_met_taken_back_for_another_reason_is_placed(
        self, work: SubmittedWork
    ) -> None:
        """The last word is "not met", and nothing failed was the base's."""
        first = _first(_met("c1", _MADE_UP))
        repeat = _answer(_not_met("c1", QUOTE_OF_NEW))

        c1 = _settle(first, work, repeat)["c1"]

        assert (c1.verdict, c1.retried, c1.quote_not_found) == (NOT_MET, True, False)
        assert (c1.quote_file, c1.quote_lines) == ("stats.py", (4, 4))


class TestACriterionCheckedByPoints:
    """Lock e: its first point not met with a place, in the list's order."""

    @staticmethod
    def _rows(*points: VerdictRecord) -> list[VerdictRecord]:
        return [
            VerdictRecord("c2", CRITERION, WeightCategory.SHOULD, NOT_MET, None),
            *points,
        ]

    @staticmethod
    def _point(
        pid: str, verdict: VerdictValue, place: tuple[str, int] | None = None
    ) -> VerdictRecord:
        if place is None:
            return VerdictRecord(pid, POINT, None, verdict, verdict)
        path, line = place
        return VerdictRecord(
            pid,
            POINT,
            None,
            verdict,
            verdict,
            quote="a line",
            quote_file=path,
            quote_lines=(line, line),
        )

    def test_the_first_unmet_point_with_a_place(self) -> None:
        rows = self._rows(
            self._point("c2.p1", MET, ("app.py", 3)),  # evidence, not a problem
            self._point("c2.p2", NOT_MET),  # no place
            self._point("c2.p3", NOT_MET, ("stats.py", 2)),
        )
        assert problem_positions(rows) == {
            "c2": Position(kind="file", value="stats.py:2")
        }

    def test_of_two_with_a_place_the_first_in_the_list(self) -> None:
        rows = self._rows(
            self._point("c2.p1", NOT_MET),
            self._point("c2.p2", NOT_MET, ("app.py", 7)),
            self._point("c2.p3", NOT_MET, ("stats.py", 2)),
        )
        assert problem_positions(rows)["c2"].value == "app.py:7"

    def test_no_point_with_a_place_leaves_the_remark_without_one(self) -> None:
        rows = self._rows(
            self._point("c2.p1", MET, ("app.py", 3)),
            self._point("c2.p2", NOT_MET),
            self._point("c2.p3", NOT_MET),
        )
        assert problem_positions(rows) == {}

    def test_through_the_settled_rows(self, work: SubmittedWork) -> None:
        answer = _answer(
            _met("c1", QUOTE_OF_NEW),
            _not_met("c2.p1"),
            _not_met("c2.p2", QUOTE_OF_AUTHORS_LINE),
            _not_met("c2.p3", QUOTE_OF_NEW),
            _met("c3", QUOTE_OF_NEW),
        )

        [remark] = _review(_settle(answer, work))[0]

        assert remark.position == Position(kind="file", value="app.py:7")


@dataclass(frozen=True)
class _Earlier:
    """An earlier verdict, as ``previous_verdicts`` returns its rows."""

    verdict: str
    quote: str | None
    quote_file: str | None = None
    submission_id: uuid.UUID = _EARLIER_SUBMISSION
    criteria_layer: str = "author"
    criteria_source_id: uuid.UUID = _LIST_ID


class TestTheSafeguard:
    """Lock f: an earlier "not met" with a place is no earlier "met"."""

    def test_an_earlier_place_still_in_the_work_keeps_nothing(
        self, work: SubmittedWork
    ) -> None:
        earlier = {"c1": _Earlier("not_met", QUOTE_OF_NEW, "stats.py")}
        assert work.find(QUOTE_OF_NEW) is not None

        c1 = _settle(_first(_not_met("c1")), work, earlier=earlier)["c1"]

        assert (c1.verdict, c1.safeguard_submission_id) == (NOT_MET, None)

    def test_an_earlier_met_is_kept_as_before(self, work: SubmittedWork) -> None:
        earlier = {"c1": _Earlier("met", QUOTE_OF_NEW, "stats.py")}

        c1 = _settle(_first(_not_met("c1", QUOTE_OF_NEW)), work, earlier=earlier)["c1"]

        assert (c1.verdict, c1.safeguard_submission_id) == (MET, _EARLIER_SUBMISSION)


class TestATextTypeKeepsNoPlace:
    """Lock g: the evaluation of a text type, its quote of a "not met" ignored."""

    _SOLUTION = "def double(n):\n    return n + n\n"

    def _work(self) -> SubmittedWork:
        return SubmittedWork([WorkFile("solution.py", self._SOLUTION, True)])

    def _answer(self, quote: str | None) -> EvaluationAnswer:
        return _answer(
            _not_met("c1", quote),
            _met("c2.p1", "def double(n):"),
            _not_met("c2.p2", quote),
            _met("c2.p3", "return n + n"),
            _not_met("c3", quote),
        )

    def test_the_rows_are_the_rows_without_the_quote(self) -> None:
        quoted = self._answer("return n + n")
        assert self._work().find("return n + n") is not None

        rows = _settle(quoted, self._work(), place_unmet=False)

        assert rows == _settle(self._answer(None), self._work(), place_unmet=False)
        assert all(
            row.quote_file is None for row in rows.values() if row.verdict is NOT_MET
        )
        # The same answer on the project's stage would keep its places.
        placed = _settle(quoted, self._work())
        assert placed["c1"].quote_file == "solution.py"

    def test_the_review_is_the_review_without_the_quote(self) -> None:
        quoted = _settle(self._answer("return n + n"), self._work(), place_unmet=False)
        bare = _settle(self._answer(None), self._work(), place_unmet=False)

        remarks, markdown = _review(quoted)

        assert (remarks, markdown) == _review(bare)
        assert all(remark.position is None for remark in remarks)
        assert "*файл" not in markdown

    def test_settle_keeps_no_place_unless_asked(self) -> None:
        """The switch is off by default: every caller but the project's stage."""
        records = settle_verdicts(
            _LIST, self._answer("return n + n"), None, work=self._work(), earlier={}
        )
        assert all(
            record.quote_file is None for record in records if record.verdict is NOT_MET
        )


class TestTheQuoteRuleOfK3Stands:
    """Lock i, in part: a "met" still stands on the student's line alone."""

    def test_a_met_on_the_students_line_of_a_changed_file(
        self, work: SubmittedWork
    ) -> None:
        c2p1 = _settle(_first(_not_met("c1")), work)["c2.p1"]

        assert (c2p1.verdict, c2p1.quote_file, c2p1.quote_lines) == (
            MET,
            "app.py",
            (8, 8),
        )

    def test_a_met_on_the_authors_line_is_the_bases(self, work: SubmittedWork) -> None:
        answer = _first(_met("c1", QUOTE_OF_AUTHORS_LINE))

        [item] = items_to_ask_again(answer, work)

        assert (item.id, item.reason) == ("c1", RepeatReason.QUOTE_FROM_BASE)


def test_nothing_switched_the_rules_version() -> None:
    """Lock i: the text reviews do not change, so the version does not."""
    assert criteria_verdicts.VERDICT_RULES_VERSION == 6
