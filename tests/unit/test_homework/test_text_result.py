"""A text task's review, laid out from its verdicts and their explanation (09b, K6).

:func:`build_text_review` is pure, so the rules of the builder are pinned
here without a database: the score and the pass are the code's, counted from
the rows (``TASK.md`` locks 1 and 2); ``passed`` never comes from the
explanation; the remarks go heaviest first, then by the criterion's number;
each remark in the section of its criterion's state since the earlier
attempt (task 12a); the review is in the explanation's language. Reading the
rows and the explanation from the database, the states counted from the two
attempts, and the refusal when either part is missing, are pinned through the
body in ``tests/integration/test_text_review_e2e_db.py``.
"""

from __future__ import annotations

import doctest

import pytest

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework import text_result
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.text_result import (
    REVIEW_PARTS_MISSING,
    FixedWords,
    build_text_review,
    link_authenticity,
)
from course_supporter.homework.verdict_explanation import (
    CriterionRemark,
    ExplanationAnswer,
)
from course_supporter.models.review_structure import (
    PassRule,
    Reference,
    Remark,
    Verdict,
)
from course_supporter.remark_kinds import RemarkState
from course_supporter.storage.submission_verdict_repository import VerdictRecord


def _criterion(
    cid: str, weight: WeightCategory, verdict: VerdictValue
) -> VerdictRecord:
    return VerdictRecord(cid, VerdictItemKind.CRITERION, weight, verdict, verdict)


def _point(pid: str, verdict: VerdictValue) -> VerdictRecord:
    return VerdictRecord(pid, VerdictItemKind.POINT, None, verdict, verdict)


def _remark(cid: str) -> CriterionRemark:
    return CriterionRemark(
        id=cid,
        what=f"Як зроблено {cid}.",
        why=f"Чому {cid} не гаразд.",
        todo=f"Як зробити {cid}.",
    )


def _explanation(
    *remarks: str, passed: bool = True, voice: str | None = "Власне слово Ментора."
) -> ExplanationAnswer:
    return ExplanationAnswer(
        passed=passed,
        why="Причина вердикту за курсом.",
        remarks=tuple(_remark(cid) for cid in remarks),
        mentor_voice=voice,
    )


MET, NOT_MET = VerdictValue.MET, VerdictValue.NOT_MET
AUTHOR = CriteriaLayer.AUTHOR
MUST, SHOULD, MAY = WeightCategory.MUST, WeightCategory.SHOULD, WeightCategory.MAY


class TestTheScore:
    """Lock 1: the score is the hand count — weights 3 / 2 / 1, points inside."""

    def test_a_criterion_with_points_counts_by_its_own_row(self) -> None:
        """c1 must (met), c2 should with points (one not met), c3 may (met).

        By hand: (3 + 1) of (3 + 2 + 1) = 4 / 6 = 66 %, rounded down. The
        point that is met does not count on its own — its criterion does.
        """
        rows = [
            _criterion("c1", MUST, MET),
            _criterion("c2", SHOULD, NOT_MET),
            _point("c2.p1", MET),
            _point("c2.p2", NOT_MET),
            _criterion("c3", MAY, MET),
        ]

        _, score = build_text_review(
            layer=AUTHOR, rows=rows, explanation=_explanation("c2"), language="ukr"
        )

        assert score == 66

    def test_everything_met_is_a_hundred(self) -> None:
        rows = [_criterion("c1", MUST, MET), _criterion("c2", MAY, MET)]

        _, score = build_text_review(
            layer=AUTHOR, rows=rows, explanation=_explanation(), language="ukr"
        )

        assert score == 100


class TestThePassIsTheCodes:
    """The builder's own rule: ``passed`` is counted, not read.

    Counted by the pass rule of 2026-11-12 on the layer it is handed; the
    rule's own locks, on either list: ``test_pass_rule.py``.
    """

    def test_an_unmet_must_fails_whatever_the_explanation_says(self) -> None:
        rows = [_criterion("c1", MUST, NOT_MET), _criterion("c2", SHOULD, MET)]

        structure, score = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c1", passed=True),
            language="ukr",
        )

        assert structure.verdict == Verdict(
            passed=False,
            why="Причина вердикту за курсом.",
            rule=PassRule(basis="author_musts_unmet", score=40, threshold=70),
        )
        assert score == 40

    def test_every_must_met_passes_whatever_the_explanation_says(self) -> None:
        # (3 + 2) of 6 -> 83: every "must" met and above 70.
        rows = [
            _criterion("c1", MUST, MET),
            _criterion("c2", SHOULD, MET),
            _criterion("c3", MAY, NOT_MET),
        ]

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c2", passed=False),
            language="ukr",
        )

        assert structure.verdict is not None
        assert structure.verdict.passed is True

    def test_a_list_without_a_must_is_passed_by_its_score_alone(self) -> None:
        rows = [_criterion("c1", SHOULD, NOT_MET), _criterion("c2", MAY, NOT_MET)]

        structure, score = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c1", "c2"),
            language="ukr",
        )

        assert structure.verdict is not None
        assert structure.verdict.passed is False
        assert structure.verdict.rule == PassRule(
            basis="no_author_musts_failed", score=0, threshold=80
        )
        assert score == 0


class TestTheRemarks:
    def test_the_heaviest_criterion_first_then_by_number(self) -> None:
        """``c10`` after ``c2``: by the number, not by the text of the id."""
        rows = [
            _criterion("c1", MAY, NOT_MET),
            _criterion("c2", SHOULD, NOT_MET),
            _criterion("c3", MUST, NOT_MET),
            _criterion("c10", SHOULD, NOT_MET),
            _criterion("c11", MUST, NOT_MET),
        ]

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c1", "c10", "c2", "c11", "c3"),
            language="ukr",
        )

        assert [remark.what for remark in structure.new_remarks] == [
            "Як зроблено c3.",
            "Як зроблено c11.",
            "Як зроблено c2.",
            "Як зроблено c10.",
            "Як зроблено c1.",
        ]

    def test_a_remark_keeps_the_three_parts_and_its_item(self) -> None:
        """The three parts, and the criterion it is about (task 12a)."""
        rows = [_criterion("c1", MUST, NOT_MET)]

        structure, _ = build_text_review(
            layer=AUTHOR, rows=rows, explanation=_explanation("c1"), language="ukr"
        )

        assert structure.new_remarks == [
            Remark(
                what="Як зроблено c1.",
                why="Чому c1 не гаразд.",
                todo="Як зробити c1.",
                item_id="c1",
            )
        ]

    def test_a_remark_on_an_item_without_a_row_is_a_defect(self) -> None:
        rows = [_criterion("c1", MUST, NOT_MET)]

        with pytest.raises(ValueError, match=r"\['c7'\]"):
            build_text_review(
                layer=AUTHOR,
                rows=rows,
                explanation=_explanation("c1", "c7"),
                language="ukr",
            )

    def test_a_remark_on_a_point_is_a_defect(self) -> None:
        """Remarks are on criteria; a point's id has no place among them."""
        rows = [_criterion("c1", MUST, NOT_MET), _point("c1.p1", NOT_MET)]

        with pytest.raises(ValueError, match=r"\['c1\.p1'\]"):
            build_text_review(
                layer=AUTHOR,
                rows=rows,
                explanation=_explanation("c1.p1"),
                language="ukr",
            )


class TestTheRestOfTheReview:
    def test_the_language_and_the_voice_are_the_explanations(self) -> None:
        rows = [_criterion("c1", MUST, MET)]

        structure, _ = build_text_review(
            layer=AUTHOR, rows=rows, explanation=_explanation(), language="eng"
        )

        assert structure.language == "eng"
        assert structure.mentor_voice == "Власне слово Ментора."
        assert structure.schema_version == "1"

    @pytest.mark.parametrize("voice", [None, "", "  \n"])
    def test_a_blank_voice_is_left_out(self, voice: str | None) -> None:
        rows = [_criterion("c1", MUST, MET)]

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation(voice=voice),
            language="ukr",
        )

        assert structure.mentor_voice is None

    def test_the_markdown_is_the_assemblers(self) -> None:
        """Nothing in the builder writes text: the one assembler does."""
        rows = [_criterion("c1", MUST, NOT_MET)]
        structure, _ = build_text_review(
            layer=AUTHOR, rows=rows, explanation=_explanation("c1"), language="ukr"
        )

        markdown = assemble_review(structure)

        assert "Причина вердикту за курсом." in markdown
        assert "Як зроблено c1." in markdown
        assert "Власне слово Ментора." in markdown

    def test_rows_without_a_criterion_have_no_review(self) -> None:
        with pytest.raises(ValueError, match="without a criterion"):
            build_text_review(
                layer=AUTHOR,
                rows=[_point("c1.p1", MET)],
                explanation=_explanation(),
                language="ukr",
            )

    def test_the_refusal_code_is_a_short_key(self) -> None:
        assert REVIEW_PARTS_MISSING == "review_parts_missing"


class TestTheCourseLinks:
    """Task 10, K6: each remark reads the links settled for its criterion."""

    def test_a_remark_reads_its_criterions_links_and_no_others(self) -> None:
        link = Reference(title="Рекурсія", segment_id="s-1")
        rows = [
            _criterion("c1", MUST, NOT_MET),
            _criterion("c2", SHOULD, NOT_MET),
        ]

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c1", "c2", passed=False),
            language="ukr",
            reading={"c2": [link]},
        )

        assert [remark.read for remark in structure.new_remarks] == [[], [link]]

    def test_without_reading_no_remark_has_any(self) -> None:
        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=[_criterion("c1", MUST, NOT_MET)],
            explanation=_explanation("c1", passed=False),
            language="ukr",
        )

        assert structure.new_remarks[0].read == []


class TestLinkAuthenticity:
    """Task 10, K6, lock g: the share of the unmet criteria that are linked."""

    def test_the_share_of_the_linked(self) -> None:
        assert link_authenticity(
            ["c1", "c2", "c3"], {"c1": ["a"], "c3": ["b", "c"]}
        ) == pytest.approx(2 / 3)

    def test_all_and_none(self) -> None:
        assert link_authenticity(["c1"], {"c1": ["a"]}) == 1.0
        assert link_authenticity(["c1"], {"c1": []}) == 0.0

    def test_nothing_unmet_is_no_claim(self) -> None:
        assert link_authenticity([], {"c1": ["a"]}) is None


class TestDocstrings:
    def test_the_examples_in_the_module_run(self) -> None:
        results = doctest.testmod(text_result, raise_on_error=False)

        assert results.attempted > 0
        assert results.failed == 0


class TestTheSectionsByState:
    """Task 12a, K2: each remark in the section of its criterion's state."""

    @staticmethod
    def _earlier(cid: str) -> CriterionRemark:
        return CriterionRemark(
            id=cid,
            what=f"Раніше {cid} не було.",
            why=f"Без {cid} не працювало.",
            todo=f"Додайте {cid}.",
            links=(f"{cid}.l1",),
        )

    @staticmethod
    def _words(*remarks: CriterionRemark, criteria: tuple[str, ...] = ()) -> FixedWords:
        return FixedWords(
            remarks={remark.id: remark for remark in remarks},
            criteria={
                cid: Criterion.model_validate(
                    {
                        "id": cid,
                        "text": f"Вимога {cid}",
                        "evidence": f"Що показує {cid}",
                        "weight": "should",
                        "check_method": "model_verdict",
                        "soft_descent": False,
                        "concepts": [],
                        "mandatory_points": [],
                    }
                )
                for cid in criteria
            },
        )

    def test_each_remark_goes_to_its_states_section(self) -> None:
        rows = [
            _criterion("c1", MUST, NOT_MET),
            _criterion("c2", SHOULD, NOT_MET),
            _criterion("c3", MAY, NOT_MET),
            _criterion("c4", SHOULD, MET),
        ]
        states = {
            "c1": RemarkState.OPEN,
            "c2": RemarkState.BROKEN,
            "c3": RemarkState.NEW,
            "c4": RemarkState.FIXED,
        }

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c3", "c2", "c1", passed=False),
            language="ukr",
            states=states,
            fixed_words=self._words(self._earlier("c4")),
        )

        assert structure.fixed == [
            Remark(
                what="Раніше c4 не було.",
                why="Без c4 не працювало.",
                todo="Додайте c4.",
                item_id="c4",
            )
        ]
        assert [remark.what for remark in structure.new_remarks] == ["Як зроблено c3."]
        assert [remark.what for remark in structure.open] == ["Як зроблено c1."]
        assert [remark.what for remark in structure.broken] == ["Як зроблено c2."]

    @pytest.mark.parametrize(
        "states",
        [None, {"c1": RemarkState.NEW, "c2": RemarkState.NEW, "c3": None}],
        ids=["no-states", "a-first-attempt"],
    )
    def test_with_nothing_to_compare_with_every_remark_is_new(
        self, states: dict[str, RemarkState | None] | None
    ) -> None:
        """As before the states: a first attempt's review is unchanged."""
        rows = [
            _criterion("c1", MUST, NOT_MET),
            _criterion("c2", SHOULD, NOT_MET),
            _criterion("c3", MAY, MET),
        ]

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c2", "c1", passed=False),
            language="ukr",
            states=states,
        )

        assert [remark.what for remark in structure.new_remarks] == [
            "Як зроблено c1.",
            "Як зроблено c2.",
        ]
        assert structure.fixed == structure.open == structure.broken == []

    def test_every_section_reads_heaviest_first_then_by_number(self) -> None:
        rows = [
            _criterion("c1", MAY, NOT_MET),
            _criterion("c2", SHOULD, NOT_MET),
            _criterion("c3", MUST, NOT_MET),
            _criterion("c4", MAY, MET),
            _criterion("c10", SHOULD, MET),
            _criterion("c11", MUST, MET),
        ]
        states = {
            "c1": RemarkState.OPEN,
            "c2": RemarkState.OPEN,
            "c3": RemarkState.OPEN,
            "c4": RemarkState.FIXED,
            "c10": RemarkState.FIXED,
            "c11": RemarkState.FIXED,
        }

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c1", "c2", "c3", passed=False),
            language="ukr",
            states=states,
            fixed_words=self._words(
                self._earlier("c4"), self._earlier("c10"), self._earlier("c11")
            ),
        )

        assert [remark.what for remark in structure.open] == [
            "Як зроблено c3.",
            "Як зроблено c2.",
            "Як зроблено c1.",
        ]
        assert [remark.what for remark in structure.fixed] == [
            "Раніше c11 не було.",
            "Раніше c10 не було.",
            "Раніше c4 не було.",
        ]

    def test_a_fixed_remark_keeps_no_reading_and_no_place_of_the_earlier_work(
        self,
    ) -> None:
        """The earlier remark is closed: its links and its place were the
        earlier work's. A criterion fixed and unconfirmed now has a remark of
        this submission too — new, with its own reading."""
        link = Reference(title="Рекурсія", segment_id="s-1")
        rows = [
            VerdictRecord(
                "c1",
                VerdictItemKind.CRITERION,
                MUST,
                NOT_MET,
                MET,
                quote_not_found=True,
            ),
        ]

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c1"),
            language="ukr",
            reading={"c1": [link]},
            states={"c1": RemarkState.FIXED},
            fixed_words=self._words(self._earlier("c1")),
        )

        [fixed] = structure.fixed
        assert (fixed.what, fixed.read, fixed.position) == (
            "Раніше c1 не було.",
            [],
            None,
        )
        [new] = structure.new_remarks
        assert (new.what, new.read) == ("Як зроблено c1.", [link])

    def test_without_an_earlier_remark_the_criterions_own_words(self) -> None:
        rows = [_criterion("c1", MUST, MET)]

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation(),
            language="ukr",
            states={"c1": RemarkState.FIXED},
            fixed_words=self._words(criteria=("c1",)),
        )

        assert structure.fixed == [
            Remark(
                what="Вимога c1", why="Вимога c1", todo="Що показує c1", item_id="c1"
            )
        ]

    def test_a_fixed_criterion_without_words_is_a_defect(self) -> None:
        with pytest.raises(ValueError, match="c1 is fixed"):
            build_text_review(
                layer=AUTHOR,
                rows=[_criterion("c1", MUST, MET)],
                explanation=_explanation(),
                language="ukr",
                states={"c1": RemarkState.FIXED},
            )

    def test_a_points_state_places_no_remark(self) -> None:
        """A remark is its criterion's: the criterion's own state places it."""
        rows = [
            _criterion("c1", MUST, NOT_MET),
            _point("c1.p1", MET),
            _point("c1.p2", NOT_MET),
        ]

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c1", passed=False),
            language="ukr",
            states={
                "c1": RemarkState.OPEN,
                "c1.p1": RemarkState.FIXED,
                "c1.p2": RemarkState.OPEN,
            },
        )

        assert structure.fixed == []
        assert [remark.what for remark in structure.open] == ["Як зроблено c1."]

    def test_every_remark_in_every_section_names_its_criterion(self) -> None:
        """Task 12a: the portal ties a student's reply to a remark by its
        ``item_id`` — in each of the four sections, the earlier attempt's
        remark under "fixed" included. The markdown does not print it."""
        rows = [
            _criterion("c1", MUST, NOT_MET),
            _criterion("c2", SHOULD, NOT_MET),
            _criterion("c3", MAY, NOT_MET),
            _criterion("c4", SHOULD, MET),
        ]

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c1", "c2", "c3", passed=False),
            language="ukr",
            states={
                "c1": RemarkState.OPEN,
                "c2": RemarkState.BROKEN,
                "c3": RemarkState.NEW,
                "c4": RemarkState.FIXED,
            },
            fixed_words=self._words(self._earlier("c4")),
        )

        assert {
            section: [remark.item_id for remark in getattr(structure, section)]
            for section in ("fixed", "new_remarks", "open", "broken")
        } == {
            "fixed": ["c4"],
            "new_remarks": ["c3"],
            "open": ["c1"],
            "broken": ["c2"],
        }
        # Not printed: the same markdown as the review without the handles.
        sections = ("fixed", "new_remarks", "open", "broken")
        without = structure.model_copy(
            update={
                section: [
                    remark.model_copy(update={"item_id": None})
                    for remark in getattr(structure, section)
                ]
                for section in sections
            }
        )
        assert assemble_review(structure) == assemble_review(without)

    def test_the_markdown_prints_the_sections_in_their_order(self) -> None:
        rows = [
            _criterion("c1", MUST, NOT_MET),
            _criterion("c2", SHOULD, NOT_MET),
            _criterion("c3", MAY, NOT_MET),
            _criterion("c4", SHOULD, MET),
        ]

        structure, _ = build_text_review(
            layer=AUTHOR,
            rows=rows,
            explanation=_explanation("c1", "c2", "c3", passed=False),
            language="ukr",
            states={
                "c1": RemarkState.OPEN,
                "c2": RemarkState.BROKEN,
                "c3": RemarkState.NEW,
                "c4": RemarkState.FIXED,
            },
            fixed_words=self._words(self._earlier("c4")),
        )
        markdown = assemble_review(structure)

        headings = [line for line in markdown.splitlines() if line.startswith("## ")]
        assert headings == [
            "## Не зараховано",
            "## Виправлено",
            "## Нові зауваження",
            "## Лишилося відкритим",
            "## Зламалося",
            "## Від Ментора",
        ]
        assert markdown.index("Раніше c4 не було.") < markdown.index("Як зроблено c3.")
