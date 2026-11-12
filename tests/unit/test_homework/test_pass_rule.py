"""The pass of a criteria list: the author's "must" criteria and 70 / 80 (2026-11-12).

The product owner's rule, locks a to g of the order, and the pure half of
lock e (the order's Ukrainian letters, transliterated: a, b, v, h, g, d, e):

* a — the author's list with a "must", every "must" met: 70 passes, 69 does
  not ("below the threshold");
* b — the author's list, one "must" not met: 95 does not pass ("the author's
  mandatory criteria are not met");
* v — the author's list, a "must" unconfirmed: it does not fail the work;
* h — the model's list, a "must" not met: 82 passes, 79 does not ("the author
  has not approved mandatory criteria … 80");
* g — the author's list without a "must": the model's rule, 80;
* e — one function: the review's builder, the explanation's facts and a recount
  from the stored rows all give the pass of :func:`pass_decision`.

Each lock is checked on the pass AND on what the student reads: the phrase the
assembler prints under the verdict, in the source language, numbers put in.
The rule through the body — a submission, the author saving the list, the
webhook — is ``tests/integration/test_pass_rule_e2e_db.py``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import get_args

import pytest
from pydantic import ValidationError

from course_supporter.config import get_settings
from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_verdicts import (
    PASS_THRESHOLD_AUTHOR_MUSTS,
    PASS_THRESHOLD_NO_AUTHOR_MUSTS,
    PassDecision,
    is_passed,
    pass_decision,
    score_percent,
)
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.text_result import build_text_review
from course_supporter.homework.verdict_explanation import (
    CriterionRemark,
    ExplanationAnswer,
    explanation_facts,
)
from course_supporter.language import get_language_registry
from course_supporter.models.review_structure import PassBasis, PassRule, Verdict
from course_supporter.phrasebook import load_phrasebook, phrases_for
from course_supporter.storage.orm import SubmissionCriterionVerdict

AUTHOR, MODEL = CriteriaLayer.AUTHOR, CriteriaLayer.MODEL
MUST, SHOULD, MAY = WeightCategory.MUST, WeightCategory.SHOULD, WeightCategory.MAY
MET, NOT_MET = VerdictValue.MET, VerdictValue.NOT_MET

# The source phrases as the owner ratified them (second edit: no word agrees
# with a number), the numbers put in.
_AUTHOR_MUSTS_MET = (
    "Ви виконали всі обов'язкові критерії автора й отримали {score} зі 100. "
    "Для завдань з обов'язковими критеріями автора поріг заліку — {threshold}, "
    "тож роботу зараховано."
)
_AUTHOR_MUSTS_UNMET = (
    "Не виконано обов'язкові критерії автора, тому роботу не зараховано. "
    "Ваш бал — {score} зі 100."
)
_AUTHOR_MUSTS_BELOW = (
    "Ви виконали всі обов'язкові критерії автора, але отримали {score} зі 100 — "
    "нижче за поріг {threshold} для завдань з обов'язковими критеріями автора, "
    "тож роботу не зараховано."
)
_NO_AUTHOR_MUSTS_PASSED = (
    "Автор не затвердив обов'язкових критеріїв для цього завдання. Для таких "
    "завдань поріг заліку — {threshold} зі 100; ваш бал — {score}, тож роботу "
    "зараховано."
)
_NO_AUTHOR_MUSTS_FAILED = (
    "Автор не затвердив обов'язкових критеріїв для цього завдання. Для таких "
    "завдань поріг заліку — {threshold} зі 100; ваш бал — {score}, тож роботу не "
    "зараховано."
)


def _rows(
    *criteria: tuple[WeightCategory, VerdictValue | str],
) -> list[SubmissionCriterionVerdict]:
    """Stored rows of criteria ``c1``, ``c2``, … — what the builder reads.

    A verdict of ``"unconfirmed"`` is a "met" no quote bore out: stored
    "not met" with the author's flag.
    """
    return [
        SubmissionCriterionVerdict(
            item_id=f"c{n}",
            item_kind=VerdictItemKind.CRITERION.value,
            weight=weight.value,
            verdict=(NOT_MET if verdict == "unconfirmed" else VerdictValue(verdict)),
            quote_not_found=verdict == "unconfirmed",
            missing=None if verdict == MET else "Немає.",
        )
        for n, (weight, verdict) in enumerate(criteria, start=1)
    ]


def _many(
    count: int, weight: WeightCategory, verdict: VerdictValue | str
) -> list[tuple[WeightCategory, VerdictValue | str]]:
    return [(weight, verdict)] * count


def _criteria(rows: Sequence[SubmissionCriterionVerdict]) -> tuple[Criterion, ...]:
    """The list the rows were judged against: a criterion per row."""
    return tuple(
        Criterion.model_validate(
            {
                "id": row.item_id,
                "text": f"Criterion {row.item_id}",
                "evidence": f"What shows {row.item_id}",
                "weight": row.weight,
                "check_method": "model_verdict",
                "soft_descent": False,
                "concepts": [],
                "mandatory_points": [],
            }
        )
        for row in rows
    )


def _explanation(
    rows: Sequence[SubmissionCriterionVerdict], *, passed: bool
) -> ExplanationAnswer:
    return ExplanationAnswer(
        passed=passed,
        why="Причина вердикту.",
        remarks=tuple(
            CriterionRemark(id=row.item_id, what="Що.", why="Чому.", todo="Як.")
            for row in rows
            if row.verdict == NOT_MET
        ),
        mentor_voice=None,
    )


def _reviewed(
    rows: Sequence[SubmissionCriterionVerdict], layer: CriteriaLayer
) -> tuple[bool, PassRule, str]:
    """The pass, the rule and the markdown the builder and the assembler give."""
    decided = pass_decision(rows, layer=layer)
    structure, score = build_text_review(
        rows=rows,
        layer=layer,
        explanation=_explanation(rows, passed=decided.passed),
        language="ukr",
    )
    assert structure.verdict is not None
    assert structure.verdict.rule is not None
    assert score == structure.verdict.rule.score
    return structure.verdict.passed, structure.verdict.rule, assemble_review(structure)


def _verdict_section(markdown: str) -> list[str]:
    """The lines of the verdict section: its heading, then its body."""
    lines = markdown.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## "))
    end = next(
        (i for i, line in enumerate(lines) if i > start and line.startswith("## ")),
        len(lines),
    )
    return [line for line in lines[start:end] if line]


# Weights 3 / 2 / 1; each case below is counted by hand in its comment.

# 7 of 10 -> 70: c1 "must" met, two "should" met, one "should" and one "may" not.
_AUTHOR_70 = _rows(
    (MUST, MET), (SHOULD, MET), (SHOULD, MET), (SHOULD, NOT_MET), (MAY, NOT_MET)
)
# 9 of 13 -> 69: two "must" and a "should" and a "may" met, two "should" not.
_AUTHOR_69 = _rows(
    (MUST, MET),
    (MUST, MET),
    (SHOULD, MET),
    (MAY, MET),
    (SHOULD, NOT_MET),
    (SHOULD, NOT_MET),
)
# 57 of 60 -> 95: one "must" not met, everything else met.
_ONE_MUST_UNMET_95 = _rows((MUST, NOT_MET), *_many(28, SHOULD, MET), (MAY, MET))
# 14 of 17 -> 82: a "must" not met beside seven "should" met.
_MUST_UNMET_82 = _rows((MUST, NOT_MET), *_many(7, SHOULD, MET))
# 19 of 24 -> 79: a "must" and a "should" not met, the rest met.
_MUST_UNMET_79 = _rows(
    (MUST, NOT_MET), (SHOULD, NOT_MET), *_many(8, SHOULD, MET), *_many(3, MAY, MET)
)


class TestTheThresholds:
    def test_they_are_the_owners(self) -> None:
        assert (PASS_THRESHOLD_AUTHOR_MUSTS, PASS_THRESHOLD_NO_AUTHOR_MUSTS) == (70, 80)


class TestLockA:
    """a: the author's "must" all met — the score decides at 70."""

    def test_seventy_passes(self) -> None:
        assert score_percent(_AUTHOR_70) == 70

        passed, rule, markdown = _reviewed(_AUTHOR_70, AUTHOR)

        assert passed is True
        assert rule == PassRule(basis="author_musts_met", score=70, threshold=70)
        assert _verdict_section(markdown)[:2] == [
            "## Зараховано",
            _AUTHOR_MUSTS_MET.format(score=70, threshold=70),
        ]

    def test_sixty_nine_does_not(self) -> None:
        assert score_percent(_AUTHOR_69) == 69

        passed, rule, markdown = _reviewed(_AUTHOR_69, AUTHOR)

        assert passed is False
        assert rule == PassRule(
            basis="author_musts_below_threshold", score=69, threshold=70
        )
        assert _verdict_section(markdown)[:2] == [
            "## Не зараховано",
            _AUTHOR_MUSTS_BELOW.format(score=69, threshold=70),
        ]


class TestLockB:
    """b: one "must" of the author's not met — no score passes."""

    def test_ninety_five_does_not_pass(self) -> None:
        assert score_percent(_ONE_MUST_UNMET_95) == 95

        passed, rule, markdown = _reviewed(_ONE_MUST_UNMET_95, AUTHOR)

        assert passed is False
        assert rule == PassRule(basis="author_musts_unmet", score=95, threshold=70)
        assert _verdict_section(markdown)[:2] == [
            "## Не зараховано",
            _AUTHOR_MUSTS_UNMET.format(score=95),
        ]


class TestLockV:
    """v: an unconfirmed "must" of the author's does not fail the work."""

    def test_unconfirmed_and_seventy_or_more_passes(self) -> None:
        # The "must" unconfirmed counts as met: 3 + 2 of 6 -> 83.
        rows = _rows((MUST, "unconfirmed"), (SHOULD, MET), (MAY, NOT_MET))
        assert score_percent(rows) == 83

        passed, rule, markdown = _reviewed(rows, AUTHOR)

        assert passed is True
        assert rule == PassRule(basis="author_musts_met", score=83, threshold=70)
        assert _AUTHOR_MUSTS_MET.format(score=83, threshold=70) in markdown

    def test_a_must_really_not_met_beside_it_still_fails(self) -> None:
        rows = _rows((MUST, "unconfirmed"), (MUST, NOT_MET), *_many(10, SHOULD, MET))

        assert pass_decision(rows, layer=AUTHOR).basis == "author_musts_unmet"


class TestLockH:
    """h: the model's "must" not met weighs in the score and blocks nothing."""

    def test_eighty_two_passes(self) -> None:
        assert score_percent(_MUST_UNMET_82) == 82

        passed, rule, markdown = _reviewed(_MUST_UNMET_82, MODEL)

        assert passed is True
        assert rule == PassRule(basis="no_author_musts_passed", score=82, threshold=80)
        assert _verdict_section(markdown)[:2] == [
            "## Зараховано",
            _NO_AUTHOR_MUSTS_PASSED.format(score=82, threshold=80),
        ]

    def test_seventy_nine_does_not(self) -> None:
        assert score_percent(_MUST_UNMET_79) == 79

        passed, rule, markdown = _reviewed(_MUST_UNMET_79, MODEL)

        assert passed is False
        assert rule == PassRule(basis="no_author_musts_failed", score=79, threshold=80)
        assert _verdict_section(markdown)[:2] == [
            "## Не зараховано",
            _NO_AUTHOR_MUSTS_FAILED.format(score=79, threshold=80),
        ]

    def test_the_must_is_shown_not_met(self) -> None:
        """Judged and shown: its remark is in the review, heaviest first."""
        _, _, markdown = _reviewed(_MUST_UNMET_82, MODEL)

        assert "## Нові зауваження" in markdown


class TestLockG:
    """g: the author's list without a "must" is passed by the model's rule."""

    @pytest.mark.parametrize(
        ("rows", "score", "passed"),
        [
            # 8 of 10 -> 80.
            (_rows(*_many(4, SHOULD, MET), (SHOULD, NOT_MET)), 80, True),
            # 3 of 4 -> 75: would pass at 70, does not at 80.
            (_rows((SHOULD, MET), (MAY, MET), (MAY, NOT_MET)), 75, False),
        ],
        ids=["80-passes", "75-does-not"],
    )
    def test_the_threshold_is_eighty(
        self, rows: list[SubmissionCriterionVerdict], score: int, passed: bool
    ) -> None:
        got, rule, markdown = _reviewed(rows, AUTHOR)

        assert got is passed
        assert (rule.score, rule.threshold) == (score, 80)
        phrase = _NO_AUTHOR_MUSTS_PASSED if passed else _NO_AUTHOR_MUSTS_FAILED
        assert phrase.format(score=score, threshold=80) in markdown

    def test_the_same_rows_on_the_models_list_read_alike(self) -> None:
        rows = _rows((SHOULD, MET), (MAY, MET), (MAY, NOT_MET))

        assert pass_decision(rows, layer=AUTHOR) == pass_decision(rows, layer=MODEL)


_EVERY_CASE = [
    _AUTHOR_70,
    _AUTHOR_69,
    _ONE_MUST_UNMET_95,
    _MUST_UNMET_82,
    _MUST_UNMET_79,
    _rows((MUST, "unconfirmed"), (SHOULD, MET), (MAY, NOT_MET)),
    _rows((SHOULD, MET), (MAY, MET), (MAY, NOT_MET)),
]


class TestLockE:
    """e, its pure half: every place that counts the pass counts it alike.

    The builder (the review and its markdown), the explanation's facts (what
    the model is told it may not contradict), and a recount from the stored
    rows. The webhook and the portal read the verdict the builder stored:
    through the body in ``tests/integration/test_pass_rule_e2e_db.py``.
    """

    @pytest.mark.parametrize("layer", list(CriteriaLayer))
    @pytest.mark.parametrize("rows", _EVERY_CASE)
    def test_one_function(
        self, rows: list[SubmissionCriterionVerdict], layer: CriteriaLayer
    ) -> None:
        decided = pass_decision(rows, layer=layer)
        built, rule, _ = _reviewed(rows, layer)
        facts = explanation_facts(_criteria(rows), rows, layer=layer)

        assert decided == PassDecision(built, rule.basis, rule.score, rule.threshold)
        assert facts.passed is built is is_passed(rows, layer=layer)
        assert facts.score == rule.score == score_percent(rows)


class TestTheStructure:
    """``Verdict.rule``: agrees with ``passed``; absent, the dump is as before."""

    @pytest.mark.parametrize(
        ("basis", "passed"),
        [
            ("author_musts_met", False),
            ("author_musts_unmet", True),
            ("author_musts_below_threshold", True),
            ("no_author_musts_passed", False),
            ("no_author_musts_failed", True),
        ],
    )
    def test_a_rule_that_disagrees_with_the_verdict_is_refused(
        self, basis: PassBasis, passed: bool
    ) -> None:
        with pytest.raises(ValidationError, match="disagrees with passed"):
            Verdict(
                passed=passed,
                why="Причина.",
                rule=PassRule(basis=basis, score=75, threshold=70),
            )

    def test_a_verdict_without_a_rule_dumps_as_before(self) -> None:
        """A test's verdict, and every review stored before 2026-11-12."""
        verdict = Verdict(passed=True, why="Причина.")

        assert verdict.model_dump(mode="json") == {"passed": True, "why": "Причина."}
        assert Verdict.model_validate({"passed": True, "why": "Причина."}) == verdict


class TestThePhrasesCountNothing:
    """The second edit of 2026-11-12: no word in a phrase agrees with a number.

    The phrasebook has no plural forms (the note at the top of ``ukr.yaml``),
    so a score is "{score} зі 100" — in each language its own "out of 100" —
    and never "{score} балів", which reads wrong for 82 or 71.
    """

    _KEYS = tuple(f"verdict.rule.{basis}" for basis in get_args(PassBasis))

    def test_the_source_puts_no_counted_noun_after_a_number(self) -> None:
        phrases = phrases_for("ukr")

        for key in self._KEYS:
            assert not re.search(r"\{(score|threshold)\} бал", phrases[key]), key
        for key in self._KEYS[:3]:
            assert "{score} зі 100" in phrases[key], key
        for key in self._KEYS[3:]:
            assert "{threshold} зі 100" in phrases[key], key

    def test_every_language_says_out_of_a_hundred(self) -> None:
        book = load_phrasebook(get_settings().phrasebook_dir)

        missing = [
            f"{code}: {key}"
            for code, phrases in book.items()
            for key in self._KEYS
            if "100" not in phrases[key].text
        ]
        assert missing == []
        assert len(book) == len(get_language_registry().languages)
