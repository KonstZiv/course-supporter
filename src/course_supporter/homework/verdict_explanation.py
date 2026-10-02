"""The explanation of a text task's verdicts: its facts and its answer (task 09b).

Purpose:
    After the evaluation stage has settled the verdicts, a second model call
    explains them to the student (``PRE-PLAN.md`` decision 13). The facts are
    the code's — the verdicts, the score, the pass — and the model may not
    contradict them (``01-IDEAL-MENTOR.md`` §1, "людяність детермінованого").
    This module holds both sides of that rule as pure functions, with no
    session and no model call: what the explanation is told, the form of what
    it answers, and the check that the answer agrees with the facts.

Interface:
    :class:`StoredVerdict` — what is read of a stored verdict row.
    :class:`JudgedItem`, :class:`CriterionFacts`, :class:`ExplanationFacts` —
    the facts, criterion by criterion, in the list's order;
    :func:`explanation_facts` builds them from the list and its rows.
    :class:`CriterionRemark`, :class:`ExplanationAnswer` — the model's answer.
    :func:`read_explanation` — the answer, checked against the facts; a
    structural retry otherwise.

The check (``PRE-FLIGHT.md`` 9.7, the check of facts; ``TASK.md`` lock 7):

* ``passed`` repeats the code's pass. An answer that says "passed" of a work
  that is not — or the other way round — is refused: the verdict's ``why``
  is written around that word, so a wrong one there is a wrong text.
* There is exactly one remark on every criterion that is not met, of any
  weight, and none on a criterion that is met or on a mandatory point: an
  unmet "must" left without a remark is the defect the lock names, and a
  remark on a met criterion tells the student to fix what is fine.
* The texts the review prints are not empty.

What the check cannot see is a claim inside the free text — "your work is
accepted" written in ``why`` of a work that is not, in one of sixty languages.
That residual risk is read by the operator on the live measurement; a
corrector over the finished text comes only if it shows (decision 5).

Replacing:
    The explanation stage depends on :func:`explanation_facts` and
    :func:`read_explanation` and on the value types; a new rule of agreement
    goes into ``_disagreements`` alone.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from course_supporter.criteria_kinds import VerdictItemKind, VerdictValue
from course_supporter.homework.criteria_form import CheckMethod, Criterion
from course_supporter.homework.criteria_verdicts import (
    QuotePlace,
    is_passed,
    score_percent,
)
from course_supporter.llm.error_categories import StructuralRetryError


class StoredVerdict(Protocol):
    """What the explanation reads of a stored verdict row.

    The row of ``storage.orm.SubmissionCriterionVerdict`` has these; the
    first four are what the score and the pass read
    (:class:`~course_supporter.homework.criteria_verdicts.ScoredItem`).
    """

    @property
    def item_id(self) -> str: ...

    @property
    def item_kind(self) -> str: ...

    @property
    def weight(self) -> str | None: ...

    @property
    def verdict(self) -> str: ...

    @property
    def quote(self) -> str | None: ...

    @property
    def quote_file(self) -> str | None: ...

    @property
    def quote_line_start(self) -> int | None: ...

    @property
    def quote_line_end(self) -> int | None: ...

    @property
    def missing(self) -> str | None: ...

    @property
    def quote_not_found(self) -> bool: ...

    @property
    def safeguard_fired(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class JudgedItem:
    """The verdict on a criterion judged on its own, or on one mandatory point.

    Each field is filled only for the verdict it belongs to, so the
    explanation is never shown what does not stand.

    Attributes:
        id: ``c3`` or ``c3.p2``.
        verdict: Met or not met.
        quote: For "met" — the line of the work it stands on.
        place: For "met" — where that line is; the lines are ``None`` for a
            document, whose lines are an artefact of extraction.
        missing: For "not met" — the sentence on what is missing, in the
            course's language; ``None`` when the model gave none.
        quote_not_found: For "not met" — the model said "met", and no line of
            the work bore its quote out, twice. Not the same as "not done":
            the explanation says so (vision-side, 2026-10-02). The model's
            quote itself is not shown: it is not in the work, so showing it
            would put words in the student's mouth.
        kept_from_earlier: For "met" — the resubmission safeguard kept the
            "met" of an earlier submission whose line is still in the work.
    """

    id: str
    verdict: VerdictValue
    quote: str | None = None
    place: QuotePlace | None = None
    missing: str | None = None
    quote_not_found: bool = False
    kept_from_earlier: bool = False


@dataclass(frozen=True, slots=True)
class CriterionFacts:
    """One criterion of the list, with its verdict.

    Attributes:
        criterion: The criterion as the list states it.
        verdict: Its verdict — the model's, or the code's from its points.
        judged: The verdict itself, for a criterion judged on its own.
        points: The verdicts on its points, in the list's order, for a
            criterion checked by mandatory points.
    """

    criterion: Criterion
    verdict: VerdictValue
    judged: JudgedItem | None
    points: tuple[JudgedItem, ...]


@dataclass(frozen=True, slots=True)
class ExplanationFacts:
    """Everything the explanation may not contradict.

    Attributes:
        passed: Every "must" criterion is met (decision 2).
        score: The share of the met criteria's weights, in whole percent.
        criteria: Every criterion of the list, in its order.
    """

    passed: bool
    score: int
    criteria: tuple[CriterionFacts, ...]

    @property
    def unmet(self) -> tuple[str, ...]:
        """The criteria that are not met, in the list's order."""
        return tuple(
            facts.criterion.id
            for facts in self.criteria
            if facts.verdict is VerdictValue.NOT_MET
        )


def explanation_facts(
    criteria: Sequence[Criterion], rows: Iterable[StoredVerdict]
) -> ExplanationFacts:
    """The facts of one submission, from its list and its verdict rows.

    The score and the pass are counted by the evaluation's own functions, as
    the review's builder counts them — nothing derived is stored.

    Raises:
        ValueError: The rows were not written against this list: a criterion
            or a point without its row, a row of an item the list does not
            have, or one item twice.
    """
    stored = list(rows)
    by_id = {row.item_id: row for row in stored}
    if len(by_id) != len(stored):
        counts = Counter(row.item_id for row in stored)
        repeated = sorted(item for item, n in counts.items() if n > 1)
        msg = f"verdict rows repeat items {repeated}"
        raise ValueError(msg)

    used: set[str] = set()

    def take(item_id: str, kind: VerdictItemKind) -> StoredVerdict:
        row = by_id.get(item_id)
        if row is None or row.item_kind != kind:
            msg = f"the list's {kind.value} {item_id} has no verdict row"
            raise ValueError(msg)
        used.add(item_id)
        return row

    facts: list[CriterionFacts] = []
    for criterion in criteria:
        row = take(criterion.id, VerdictItemKind.CRITERION)
        if criterion.check_method is CheckMethod.MANDATORY_POINTS:
            points = tuple(
                _judged(take(point.id, VerdictItemKind.POINT))
                for point in criterion.mandatory_points
            )
            facts.append(
                CriterionFacts(criterion, VerdictValue(row.verdict), None, points)
            )
        else:
            facts.append(
                CriterionFacts(criterion, VerdictValue(row.verdict), _judged(row), ())
            )

    foreign = sorted(by_id.keys() - used)
    if foreign:
        msg = f"verdict rows of items the list does not have: {foreign}"
        raise ValueError(msg)
    return ExplanationFacts(
        passed=is_passed(stored), score=score_percent(stored), criteria=tuple(facts)
    )


def _judged(row: StoredVerdict) -> JudgedItem:
    """The verdict of one row, with only the fields its verdict uses."""
    verdict = VerdictValue(row.verdict)
    if verdict is VerdictValue.NOT_MET:
        return JudgedItem(
            id=row.item_id,
            verdict=verdict,
            missing=row.missing,
            quote_not_found=row.quote_not_found,
        )
    lines = (
        (row.quote_line_start, row.quote_line_end)
        if row.quote_line_start is not None and row.quote_line_end is not None
        else None
    )
    return JudgedItem(
        id=row.item_id,
        verdict=verdict,
        quote=row.quote,
        place=QuotePlace(row.quote_file, lines) if row.quote_file else None,
        # A row the safeguard kept also carries quote_not_found — the model's
        # own quote did not stand (task 09b, K2) — but the verdict that stands
        # is "met", on the earlier line, and that is what is explained.
        kept_from_earlier=row.safeguard_fired,
    )


class CriterionRemark(BaseModel):
    """The remark on one criterion that is not met, in the contrasting form.

    The three parts of ``01-IDEAL-MENTOR.md`` §1: how it is done now, what the
    problem is, how it should be — the parts ``what``, ``why`` and ``todo`` of
    the review's remark (``models/review_structure.Remark``), which the
    builder fills from this. There is no place for reading: links to the
    course are resolved by code (task 10), never written by the model.

    Every field is required and none has a default, so the strict schema on
    the wire lists them all.

    Attributes:
        id: The criterion, as the list names it (``c3``).
        what: How it is done in the work now — or that it is not there.
        why: What the problem is.
        todo: How it should be, and why that solves the problem.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    what: str
    why: str
    todo: str


class ExplanationAnswer(BaseModel):
    """The model's explanation of the verdicts, as the stage keeps it.

    The review's builder turns it into the review's structure
    (``homework/text_result.py``).

    Attributes:
        passed: The code's pass, repeated: the check compares the two.
        why: The verdict's reason, in the voice "by the course": what the
            author required and how the work meets it, what is done well and
            where the student went beyond the assignment (``PRE-FLIGHT.md``
            9.7, the place of praise).
        remarks: One per criterion that is not met.
        mentor_voice: The Mentor's own judgement, where it differs from the
            course or goes well beyond it; ``None`` when there is none.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    why: str
    remarks: tuple[CriterionRemark, ...]
    mentor_voice: str | None


def read_explanation(content: str, facts: ExplanationFacts) -> ExplanationAnswer:
    """Read the model's explanation and refuse one that disagrees with the facts.

    Every disagreement is named at once, so one retry can mend them all.

    Args:
        content: The model's response text.
        facts: What the explanation may not contradict.

    Raises:
        StructuralRetryError: The answer is not valid JSON of this form, or
            disagrees with the facts (see the module).

    >>> facts = ExplanationFacts(passed=False, score=0, criteria=())
    >>> try:
    ...     read_explanation(
    ...         '{"passed": true, "why": "Добре.", "remarks": [], '
    ...         '"mentor_voice": null}',
    ...         facts,
    ...     )
    ... except StructuralRetryError as refusal:
    ...     print(refusal.feedback)  # doctest: +ELLIPSIS
    passed must be false: ...
    """
    try:
        answer = ExplanationAnswer.model_validate_json(content)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first.get("loc", ()))
        msg = (
            f"{first.get('msg', 'validation error')} (field: {loc or '<root>'}). "
            "Regenerate the response with valid JSON matching the schema."
        )
        raise StructuralRetryError(msg) from exc

    problems = _disagreements(answer, facts)
    if problems:
        raise StructuralRetryError(
            " ".join([*problems, "Regenerate the whole JSON object."])
        )
    return answer


def _disagreements(answer: ExplanationAnswer, facts: ExplanationFacts) -> list[str]:
    """What in ``answer`` contradicts ``facts`` or leaves a printed text empty."""
    problems: list[str] = []
    if answer.passed != facts.passed:
        problems.append(
            f"passed must be {json.dumps(facts.passed)}: the code counted it from "
            "the verdicts, and the explanation repeats it and never changes it; "
            "the text of why must agree with it."
        )

    counts = Counter(remark.id for remark in answer.remarks)
    wanted = set(facts.unmet)
    missing = [cid for cid in facts.unmet if cid not in counts]
    unexpected = sorted(counts.keys() - wanted)
    repeated = sorted(cid for cid, n in counts.items() if n > 1)
    if missing or unexpected or repeated:
        problems.append(
            "remarks must cover exactly the criteria that are not met: "
            f"missing {missing}, unexpected {unexpected}, repeated {repeated}. "
            f"Give one remark for each of {list(facts.unmet)} and no others."
        )

    blank = [] if answer.why.strip() else ["why"]
    for remark in answer.remarks:
        blank.extend(
            f"remarks[{remark.id}].{part}"
            for part in ("what", "why", "todo")
            if not getattr(remark, part).strip()
        )
    if blank:
        problems.append(f"these texts are empty: {blank}. Write each of them.")
    return problems
