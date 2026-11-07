"""Verdicts on a task's criteria: from the model's answer to the score (task 09b).

Purpose:
    The evaluation stage of a text task asks a model for a verdict on every
    criterion of the list in force and on every mandatory point. Everything
    after the answer is the code's, and lives here: pure functions with no
    session, no clock and no model call, so the same answer on the same work
    against the same list gives the same verdicts, score and pass every time
    (``TASK.md`` section 5, lock 9). The stage reads the model's answer and
    settles the verdicts to store with them; the review's builder recounts the
    score and the pass from the stored rows with the same two functions, so
    nothing derived is ever stored beside its source.

Interface:
    :class:`ItemVerdict`, :class:`EvaluationAnswer` — the model's answer.
    :func:`judged_items` — what the model is asked about.
    :func:`read_answer` — the answer as a whole, checked; a structural retry
    otherwise.
    :class:`WorkFile`, :class:`SubmittedWork`, :class:`QuotePlace` — the work,
    and where in it a quote stands.
    :func:`items_to_ask_again`, :class:`RepeatItem`, :class:`RepeatReason` —
    the verdicts that cannot stand as given, each with its reason: the stage
    asks once more about these alone.
    :class:`EarlierVerdict` — what the resubmission safeguard reads of a
    verdict on an earlier submission.
    :func:`settle_verdicts` — the verdict rows to store.
    :class:`ScoredItem`, :func:`score_percent`, :func:`is_passed` — the score
    and the pass, from rows.

The rules (``PRE-PLAN.md`` decisions 2, 3, 4, 7, 11; ``PRE-FLIGHT.md`` 9.4,
9.8, 9.10; the item-level repeat, ratified 2026-10-02; the whole line and
"unconfirmed", ratified 2026-11-07):

* A "met" stands on a quote of one line and at most :data:`MAX_QUOTE_CHARS`
  characters that IS in the work. Quote and work are compared in NFC,
  without the byte-order mark and without any whitespace, inside one file's
  text at a time. A quote of fewer than :data:`MIN_QUOTE_CHARS`
  non-whitespace characters is not found — a quote that short is found in
  any work and proves nothing — unless it is a WHOLE line of a file with
  lines: then the line is all the evidence there is (``.venv/`` in a
  ``.gitignore``), and the quote is placed on that line. A blank line is
  never evidence. A "not met" stands on a sentence on what is missing.
* Only a defect of the answer as a whole — not JSON of this form, an item
  missing, unknown or repeated — repeats the whole answer. A verdict that
  cannot stand as given — a "met" without a quote that stands, a "not met"
  without a sentence — is asked about once more on its own, with its reason.
  The field a verdict does not use — the sentence of a "met", the quote of a
  "not met" — is ignored, never a defect.
* A "met" that still does not stand after the repeat — the repeat gave
  another quote that does not stand, or took back as "not met" a "met" whose
  quote was too short — is UNCONFIRMED: it reads "not met", flagged for the
  author (``quote_not_found``), unless the safeguard keeps an earlier "met".
  The model said "done", and the check found no line to bear it out: that is
  not the same as "not done". A "met" taken back after any other defect of
  its quote — not found in the work, say — is "not met": the model most
  likely erred first. A "not met" given in the first answer is "not met";
  one still without a sentence reads "not met" without one.
* A criterion checked by mandatory points is met when every point is, and
  unconfirmed when every point is met or unconfirmed — one at least
  unconfirmed.
* The score is the share of the weights of the met criteria (3 / 2 / 1) in
  the sum of all weights, in whole percent, rounded DOWN — 100 only when
  everything is met or unconfirmed. Passed means every "must" criterion is
  met or unconfirmed; a list with no "must" is passed.
* The resubmission safeguard: a "not met" on an item whose LATEST earlier
  verdict on the same list was "met", and whose quote is still in the work
  word for word, stays "met" — and says so in its row.

Replacing:
    The stage depends on the functions above and on their value types; a new
    rule replaces a function's body, not its signature. The score formula is
    :func:`score_percent` alone, the matching of a quote is
    :meth:`SubmittedWork.find` alone, what lets a single verdict stand is
    ``_repeat_reason`` alone, which of the model's words an item is settled
    on is ``_word_to_settle`` alone, the safeguard's condition is
    ``_kept_by_safeguard`` alone.
"""

from __future__ import annotations

import bisect
import unicodedata
import uuid
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from course_supporter.criteria_kinds import (
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.criteria_form import (
    WEIGHT_NUMBERS,
    CheckMethod,
    Criterion,
)
from course_supporter.homework.criteria_list_service import CriteriaInForce
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.storage.submission_verdict_repository import VerdictRecord

MAX_QUOTE_CHARS: Final = 200
"""Characters of a quote at most (``PRE-FLIGHT.md`` 9.4).

A quote is evidence the author and the student glance at, not a passage; a
longer one is asked about again.
"""

MIN_QUOTE_CHARS: Final = 8
"""Non-whitespace characters of a quote at least (``PRE-FLIGHT.md`` 9.4).

A shorter quote counts as not found and is asked about again: ``x = 1`` is in
almost any work, so finding it proves nothing about the criterion. A shorter
quote that is a whole line of the work is found (:meth:`SubmittedWork.find`).
"""

_BOM: Final = chr(0xFEFF)


class ItemVerdict(BaseModel):
    """The model's verdict on one criterion or one mandatory point.

    Every field is required and none has a default, so the model's JSON
    schema lists them all as required — the form a strict schema on the wire
    (task 09a) asks for. ``quote`` and ``missing`` are both present; the one
    ``verdict`` does not use is ignored (:func:`settle_verdicts`).

    Attributes:
        id: The item as the list names it: ``c3`` or ``c3.p2``.
        verdict: Met or not met.
        quote: For "met" — one line of the work, copied word for word.
        missing: For "not met" — one sentence on what is missing, in the
            language of the course.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    verdict: VerdictValue
    quote: str | None
    missing: str | None


class EvaluationAnswer(BaseModel):
    """The model's whole answer: one verdict per item it was asked about.

    A list rather than a mapping keyed by identifier: a strict schema names
    its keys in advance, and these keys are the list's data.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdicts: tuple[ItemVerdict, ...]


def judged_items(criteria: Sequence[Criterion]) -> tuple[str, ...]:
    """The items the model gives a verdict on, in the list's order.

    A criterion checked by mandatory points is judged through its points
    alone: its own verdict is the code's (decision 3), so the model is not
    asked for one it could contradict. A ``code_test`` criterion is judged by
    a model verdict while nothing runs code tests — its ``soft_descent``
    (``homework/criteria_form.py``, :class:`CheckMethod`).
    """
    items: list[str] = []
    for criterion in criteria:
        if criterion.check_method is CheckMethod.MANDATORY_POINTS:
            items.extend(point.id for point in criterion.mandatory_points)
        else:
            items.append(criterion.id)
    return tuple(items)


def read_answer(content: str, expected: Collection[str]) -> EvaluationAnswer:
    """Read the model's answer and refuse one the stage cannot use as a whole.

    A schema holds the form, not the content: under a strict schema a model
    still invented a criterion's identifier (``09a-schema/LIVE-SCHEMA-CHECK.md``).
    So the code checks that the answer is JSON of this form and gives one
    verdict on each expected item, no other and none twice — and refuses with
    feedback the router appends to its retry of the whole answer.

    Nothing about a single verdict is refused here. A verdict that cannot
    stand as given is asked about again on its own (:func:`items_to_ask_again`)
    instead of paying for the whole answer once more.

    Args:
        content: The model's response text.
        expected: The items asked about — :func:`judged_items` for the first
            request, the ids of :func:`items_to_ask_again` for the repeat.

    Returns:
        The answer, its verdicts in the model's order.

    Raises:
        StructuralRetryError: The answer is not valid JSON of this form, or an
            expected item is missing, unknown or repeated.

    >>> try:
    ...     read_answer('{"verdicts": []}', ["c1", "c2.p1"])
    ... except StructuralRetryError as refusal:
    ...     print(refusal.feedback)  # doctest: +ELLIPSIS
    verdicts must cover exactly the items asked about: missing ['c1', 'c2.p1'], ...
    """
    try:
        answer = EvaluationAnswer.model_validate_json(content)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first.get("loc", ()))
        msg = (
            f"{first.get('msg', 'validation error')} (field: {loc or '<root>'}). "
            "Regenerate the response with valid JSON matching the schema."
        )
        raise StructuralRetryError(msg) from exc

    counts = Counter(item.id for item in answer.verdicts)
    wanted = set(expected)
    missing = sorted(wanted - counts.keys())
    unexpected = sorted(counts.keys() - wanted)
    repeated = sorted(i for i, n in counts.items() if n > 1)
    if missing or unexpected or repeated:
        msg = (
            "verdicts must cover exactly the items asked about: "
            f"missing {missing}, unexpected {unexpected}, repeated {repeated}. "
            f"Regenerate with one verdict for each of {sorted(wanted)} and no "
            "others."
        )
        raise StructuralRetryError(msg)
    return answer


def _given(text: str | None) -> str | None:
    """``text`` without its outer whitespace, or ``None`` when nothing is left."""
    stripped = (text or "").strip()
    return stripped or None


@dataclass(frozen=True, slots=True)
class WorkFile:
    """One file of a submitted work, as the review reads it.

    Attributes:
        name: The archive member's path, or the submitted file's own name.
        text: The file's text as read; the search normalizes it, not the
            caller.
        has_lines: ``False`` for a document (docx, pdf): its lines are an
            artefact of text extraction, not what the student sees, so a quote
            in it is placed by the file alone (``PRE-FLIGHT.md`` 9.3).
    """

    name: str
    text: str
    has_lines: bool


@dataclass(frozen=True, slots=True)
class QuotePlace:
    """Where a quote stands in the work.

    Attributes:
        file: The :attr:`WorkFile.name` of the file it is in.
        lines: Its first and last physical line in that file, from 1 — or
            ``None`` for a file without lines (:attr:`WorkFile.has_lines`).
    """

    file: str
    lines: tuple[int, int] | None


def _ignored(char: str) -> bool:
    """A character the comparison of a quote with the work does not see."""
    return char.isspace() or char == _BOM


def _squeezed(text: str) -> str:
    """``text`` as quotes are compared: NFC, no byte-order mark, no whitespace."""
    return "".join(c for c in unicodedata.normalize("NFC", text) if not _ignored(c))


class _SearchableFile:
    """One file squeezed for the search, with where each of its lines begins."""

    __slots__ = ("file", "line_starts", "squeezed")

    def __init__(self, file: WorkFile) -> None:
        self.file = file
        kept: list[str] = []
        # line_starts[k] is how many squeezed characters come before line k+1,
        # so a squeezed offset's line is a bisection, with no table per
        # character. NFC never composes across a newline, so normalizing the
        # whole text keeps every newline where it was.
        self.line_starts = [0]
        for char in unicodedata.normalize("NFC", file.text):
            if char == "\n":
                self.line_starts.append(len(kept))
            elif not _ignored(char):
                kept.append(char)
        self.squeezed = "".join(kept)

    def line_of(self, offset: int) -> int:
        """The physical line, from 1, of the squeezed character at ``offset``."""
        return bisect.bisect_right(self.line_starts, offset)

    def whole_line(self, needle: str) -> int | None:
        """The first physical line, from 1, that is ``needle`` and nothing else.

        Both squeezed. ``None`` for an empty needle — a blank line proves
        nothing — and for a file without lines, whose lines are an artefact of
        text extraction (:attr:`WorkFile.has_lines`), not lines the student
        wrote.
        """
        if not needle or not self.file.has_lines:
            return None
        ends = [*self.line_starts[1:], len(self.squeezed)]
        for number, (start, end) in enumerate(
            zip(self.line_starts, ends, strict=True), start=1
        ):
            if end - start == len(needle) and self.squeezed.startswith(needle, start):
                return number
        return None

    def place_of(self, needle: str) -> QuotePlace | None:
        """Where the squeezed ``needle`` first stands in this file, or ``None``."""
        if len(needle) < MIN_QUOTE_CHARS:
            line = self.whole_line(needle)
            return None if line is None else QuotePlace(self.file.name, (line, line))
        start = self.squeezed.find(needle)
        if start < 0:
            return None
        if not self.file.has_lines:
            return QuotePlace(file=self.file.name, lines=None)
        first = self.line_of(start)
        last = self.line_of(start + len(needle) - 1)
        return QuotePlace(file=self.file.name, lines=(first, last))


class SubmittedWork:
    """A submitted work, searchable for the quotes verdicts stand on.

    Each file is searched on its own, in the order given: a quote never spans
    two files, and the frames and notes the review's prompt puts around the
    files (their names, the list of what was not opened) are not text of the
    work, so a quote of them is not found.

    >>> work = SubmittedWork(
    ...     [WorkFile("main.py", "def double(n):\\r\\n    return n * 2\\n", True)]
    ... )
    >>> work.find("return   n*2")
    QuotePlace(file='main.py', lines=(2, 2))
    >>> work.find("n * 2") is None
    True
    >>> SubmittedWork([WorkFile(".gitignore", "*.pyc\\n  .venv/\\n", True)]).find(
    ...     ".venv/"
    ... )
    QuotePlace(file='.gitignore', lines=(2, 2))
    """

    def __init__(self, files: Sequence[WorkFile]) -> None:
        self._files = tuple(_SearchableFile(file) for file in files)

    def find(self, quote: str) -> QuotePlace | None:
        """Where ``quote`` first stands in the work, or ``None``.

        Compared without whitespace, in NFC and without the byte-order mark on
        both sides, so line breaks, indentation, CRLF and a decomposed accent
        do not hide a quote the student did write. A quote with fewer than
        :data:`MIN_QUOTE_CHARS` non-whitespace characters is found only as a
        whole line of a file with lines — compared the same way — and placed
        on that line; anywhere else it is not found.
        """
        needle = _squeezed(quote)
        for searchable in self._files:
            place = searchable.place_of(needle)
            if place is not None:
                return place
        return None


class RepeatReason(StrEnum):
    """Why a verdict cannot stand as given and is asked about once more."""

    NO_QUOTE = "no_quote"
    QUOTE_NOT_ONE_LINE = "quote_not_one_line"
    QUOTE_TOO_LONG = "quote_too_long"
    QUOTE_TOO_SHORT = "quote_too_short"
    """Too short to prove anything: counted as a quote not found (9.4)."""
    QUOTE_NOT_FOUND = "quote_not_found"
    NO_SENTENCE = "no_sentence"


# What the repeated request tells the model about each item: one defect, said
# so that the model can mend it. English, as every instruction to a model is.
_REPEAT_FEEDBACK: Final[Mapping[RepeatReason, str]] = MappingProxyType(
    {
        RepeatReason.NO_QUOTE: "the verdict is 'met' but gives no quote",
        RepeatReason.QUOTE_NOT_ONE_LINE: "the quote spans several lines",
        RepeatReason.QUOTE_TOO_LONG: (
            f"the quote is longer than {MAX_QUOTE_CHARS} characters"
        ),
        RepeatReason.QUOTE_TOO_SHORT: (
            f"the quote has fewer than {MIN_QUOTE_CHARS} characters besides "
            "spaces, too few to show anything"
        ),
        RepeatReason.QUOTE_NOT_FOUND: "the quote is not in the work word for word",
        RepeatReason.NO_SENTENCE: (
            "the verdict is 'not_met' but gives no sentence on what is missing"
        ),
    }
)


@dataclass(frozen=True, slots=True)
class RepeatItem:
    """One verdict to ask about again, and why.

    Attributes:
        id: The item, as the list names it.
        reason: What keeps its verdict from standing as given.
    """

    id: str
    reason: RepeatReason

    @property
    def feedback(self) -> str:
        """The reason in words, for the repeated request to put to the model."""
        return _REPEAT_FEEDBACK[self.reason]


def _standing_quote(
    quote: str | None, work: SubmittedWork
) -> QuotePlace | RepeatReason:
    """Where a "met"'s quote stands in the work, or why the "met" does not stand."""
    text = _given(quote)
    if text is None:
        return RepeatReason.NO_QUOTE
    if len(text.splitlines()) > 1:
        return RepeatReason.QUOTE_NOT_ONE_LINE
    if len(unicodedata.normalize("NFC", text)) > MAX_QUOTE_CHARS:
        return RepeatReason.QUOTE_TOO_LONG
    place = work.find(text)
    if place is not None:
        return place
    # A short quote the search did not take as a whole line is named as too
    # short: a longer quote is what mends it, not another place to look.
    if len(_squeezed(text)) < MIN_QUOTE_CHARS:
        return RepeatReason.QUOTE_TOO_SHORT
    return RepeatReason.QUOTE_NOT_FOUND


def _repeat_reason(item: ItemVerdict, work: SubmittedWork) -> RepeatReason | None:
    """Why ``item`` cannot stand as given, or ``None`` when it can."""
    if item.verdict is VerdictValue.NOT_MET:
        return RepeatReason.NO_SENTENCE if _given(item.missing) is None else None
    standing = _standing_quote(item.quote, work)
    return standing if isinstance(standing, RepeatReason) else None


def items_to_ask_again(
    answer: EvaluationAnswer, work: SubmittedWork
) -> tuple[RepeatItem, ...]:
    """The verdicts that cannot stand as given, each with its reason.

    A "met" without a quote that stands — none, more than one line, over
    :data:`MAX_QUOTE_CHARS` characters, too short or not in the work — and a
    "not met" without a sentence. The stage asks once more about these alone,
    naming each one's reason (decision 11, widened to every defect of a single
    verdict on 2026-10-02); every other item keeps its first answer. The field
    a verdict does not use is never a reason.
    """
    return tuple(
        RepeatItem(item.id, reason)
        for item in answer.verdicts
        if (reason := _repeat_reason(item, work)) is not None
    )


class EarlierVerdict(Protocol):
    """What the safeguard reads of a verdict on an earlier submission.

    A stored row (``storage.orm.SubmissionCriterionVerdict``), as
    ``SubmissionVerdictRepository.previous_verdicts`` returns it, has all of
    these.
    """

    @property
    def submission_id(self) -> uuid.UUID: ...

    @property
    def criteria_layer(self) -> str: ...

    @property
    def criteria_source_id(self) -> uuid.UUID: ...

    @property
    def verdict(self) -> str: ...

    @property
    def quote(self) -> str | None: ...


def settle_verdicts(
    criteria: CriteriaInForce,
    answer: EvaluationAnswer,
    repeat: EvaluationAnswer | None,
    *,
    work: SubmittedWork,
    earlier: Mapping[str, EarlierVerdict],
) -> tuple[VerdictRecord, ...]:
    """The verdict rows of a submission, from the model's answers.

    For every item, the model's last word: the repeat's for the items it was
    asked about again, the first answer's for the rest — except a "met" whose
    quote was too short and which the repeat took back, settled on that
    "met" (:func:`_word_to_settle`). Then, item by item:

    1. "met" on a quote that stands (:func:`items_to_ask_again`) — met,
       placed;
    2. otherwise the resubmission safeguard: the latest earlier verdict on
       this item, if it was judged against THIS list (identifiers are stable
       within one list only — task 08, decision 21), said "met", and its quote
       is still in the work — met, kept, with the earlier submission named;
    3. otherwise not met — and a "met" whose quote did not stand is flagged
       for the author: unconfirmed.

    The field a verdict does not use is ignored and not stored: the sentence
    of a "met", the quote of a "not met". A "not met" without a sentence is
    stored without one.

    A criterion checked by mandatory points is then met when all of its
    points are, and unconfirmed when each is met or unconfirmed — after the
    safeguard, so a kept point counts.

    Args:
        criteria: The list in force the answers were given against.
        answer: The model's first answer, checked by :func:`read_answer`.
        repeat: The answer to the repeated request, or ``None`` when nothing
            was asked again.
        work: The work the answers are about.
        earlier: The latest earlier verdict of each item, by item id
            (``SubmissionVerdictRepository.previous_verdicts``).

    Returns:
        One record per criterion and per mandatory point, in the list's
        order: a criterion first, then its points.

    Raises:
        ValueError: The answers do not cover exactly the list's items.
    """
    expected = judged_items(criteria.criteria)
    first = {item.id: item for item in answer.verdicts}
    again = {item.id: item for item in repeat.verdicts} if repeat is not None else {}
    if first.keys() != set(expected) or not again.keys() <= set(expected):
        msg = f"the answers do not cover exactly the items {list(expected)}"
        raise ValueError(msg)

    def judged(
        item_id: str, kind: VerdictItemKind, weight: WeightCategory | None
    ) -> VerdictRecord:
        return _judged(
            _word_to_settle(first[item_id], again.get(item_id), work),
            kind=kind,
            weight=weight,
            retried=item_id in again,
            criteria=criteria,
            work=work,
            earlier=earlier,
        )

    records: list[VerdictRecord] = []
    for criterion in criteria.criteria:
        if criterion.check_method is not CheckMethod.MANDATORY_POINTS:
            records.append(
                judged(criterion.id, VerdictItemKind.CRITERION, criterion.weight)
            )
            continue
        points = [
            judged(point.id, VerdictItemKind.POINT, None)
            for point in criterion.mandatory_points
        ]
        records.append(_from_points(criterion, points))
        records.extend(points)
    return tuple(records)


def _word_to_settle(
    first: ItemVerdict, repeat: ItemVerdict | None, work: SubmittedWork
) -> ItemVerdict:
    """The verdict an item is settled on: the model's last word, but one case.

    A "met" whose quote was too short (:attr:`RepeatReason.QUOTE_TOO_SHORT`),
    taken back as "not met" on the repeat, is settled on that first "met" —
    so it reads unconfirmed, as a "met" whose second quote did not stand
    either, and not "not met" (ratified 2026-11-07). The evidence is most
    likely in the work and the rule on a quote's length refused it: the
    repeat answered that refusal, not a question about the work — in the
    student's problem 2 a line too short to quote at more length was taken
    back with a sentence on the quote's length. That sentence is about the
    check, not the work, so it is not stored, and the explanation, which
    reads the stored rows, is told the state alone, never the check's
    mechanics. A short quote the work does not have at all is named too
    short as well (``_standing_quote`` names every short miss so), so its
    take-back reads unconfirmed too.

    A "met" taken back after any other defect of its quote — a quote not in
    the work, none, of several lines, too long — is "not met", as before the
    decision: the model most likely erred the first time (narrowed by the
    Mentor session, 2026-11-07). Every other item is settled on its last
    word too: a "not met" of the first answer stays one, and the repeat's
    word on a "not met" without a sentence stands.
    """
    if repeat is None:
        return first
    took_back = repeat.verdict is VerdictValue.NOT_MET
    too_short = (
        first.verdict is VerdictValue.MET
        and _repeat_reason(first, work) is RepeatReason.QUOTE_TOO_SHORT
    )
    return first if took_back and too_short else repeat


def _unconfirmed(item: ScoredItem) -> bool:
    """The model said "met" and no line of the work bore it out: unconfirmed.

    Not "not done" — the check could not confirm it. The flag for the author
    is ``quote_not_found`` on a row that reads "not met"; one the safeguard
    kept reads "met" and is met.
    """
    return item.verdict == VerdictValue.NOT_MET and item.quote_not_found


def _from_points(
    criterion: Criterion, points: Sequence[VerdictRecord]
) -> VerdictRecord:
    """The code's verdict on a criterion checked by mandatory points.

    Met when every point is; unconfirmed — "not met" with the author's flag,
    as an unconfirmed point reads — when every point is met or unconfirmed;
    not met otherwise (decision 3; unconfirmed since 2026-11-07).
    """
    every_met = all(point.verdict is VerdictValue.MET for point in points)
    unconfirmed = not every_met and all(
        point.verdict is VerdictValue.MET or _unconfirmed(point) for point in points
    )
    return VerdictRecord(
        item_id=criterion.id,
        item_kind=VerdictItemKind.CRITERION,
        weight=criterion.weight,
        verdict=VerdictValue.MET if every_met else VerdictValue.NOT_MET,
        model_verdict=None,
        quote_not_found=unconfirmed,
    )


def _judged(
    item: ItemVerdict,
    *,
    kind: VerdictItemKind,
    weight: WeightCategory | None,
    retried: bool,
    criteria: CriteriaInForce,
    work: SubmittedWork,
    earlier: Mapping[str, EarlierVerdict],
) -> VerdictRecord:
    """The record of one item the model judged — steps 1 to 3 of settling."""
    said_met = item.verdict is VerdictValue.MET
    quote = _given(item.quote) if said_met else None
    missing = None if said_met else _given(item.missing)
    if said_met:
        standing = _standing_quote(quote, work)
        if isinstance(standing, QuotePlace):
            return VerdictRecord(
                item_id=item.id,
                item_kind=kind,
                weight=weight,
                verdict=VerdictValue.MET,
                model_verdict=item.verdict,
                quote=quote,
                quote_file=standing.file,
                quote_lines=standing.lines,
                retried=retried,
            )
    kept = _kept_by_safeguard(item.id, criteria=criteria, work=work, earlier=earlier)
    if kept is not None:
        before, kept_place = kept
        # The row's quote is the one the verdict now stands on — the earlier
        # submission's, found in this work; the model's own sentence (or its
        # unfound quote's flag) stays beside it as the proposal (KD20).
        return VerdictRecord(
            item_id=item.id,
            item_kind=kind,
            weight=weight,
            verdict=VerdictValue.MET,
            model_verdict=item.verdict,
            quote=before.quote,
            quote_file=kept_place.file,
            quote_lines=kept_place.lines,
            missing=missing,
            retried=retried,
            quote_not_found=said_met,
            safeguard_submission_id=before.submission_id,
        )
    return VerdictRecord(
        item_id=item.id,
        item_kind=kind,
        weight=weight,
        verdict=VerdictValue.NOT_MET,
        model_verdict=item.verdict,
        quote=quote,
        missing=missing,
        retried=retried,
        quote_not_found=said_met,
    )


def _kept_by_safeguard(
    item_id: str,
    *,
    criteria: CriteriaInForce,
    work: SubmittedWork,
    earlier: Mapping[str, EarlierVerdict],
) -> tuple[EarlierVerdict, QuotePlace] | None:
    """The earlier "met" the safeguard keeps for ``item_id``, and where its quote is."""
    before = earlier.get(item_id)
    if before is None or before.verdict != VerdictValue.MET or before.quote is None:
        return None
    if (
        before.criteria_layer != criteria.layer
        or before.criteria_source_id != criteria.source_id
    ):
        return None
    place = work.find(before.quote)
    return None if place is None else (before, place)


class ScoredItem(Protocol):
    """What the score and the pass read of a verdict row.

    Both a :class:`VerdictRecord` and a stored row
    (``storage.orm.SubmissionCriterionVerdict``) have these, so the builder of
    the review recounts from what is stored exactly as the stage counted.
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
    def quote_not_found(self) -> bool: ...


class _Outcome(NamedTuple):
    """One criterion as the score and the pass read it."""

    weight: WeightCategory
    met: bool
    unconfirmed: bool


def _criteria_outcomes(items: Iterable[ScoredItem]) -> list[_Outcome]:
    """Each criterion's weight and state; points are left out."""
    outcomes: list[_Outcome] = []
    for item in items:
        if item.item_kind != VerdictItemKind.CRITERION:
            continue
        if item.weight is None:
            msg = f"criterion {item.item_id} has no weight"
            raise ValueError(msg)
        outcomes.append(
            _Outcome(
                weight=WeightCategory(item.weight),
                met=item.verdict == VerdictValue.MET,
                unconfirmed=_unconfirmed(item),
            )
        )
    return outcomes


def score_percent(items: Iterable[ScoredItem]) -> int:
    """The share of the met criteria's weights, in whole percent, rounded down.

    Rounded down so a score never overstates and 100 means everything is met
    — or unconfirmed (below) — (``PRE-FLIGHT.md`` 9.10); the portal reads 100
    as "correct". Points do not count on their own — their criterion does.

    Raises:
        ValueError: There is no criterion among ``items``, or one has no
            weight.

    >>> def criterion(weight, verdict):
    ...     return VerdictRecord(
    ...         "c1", VerdictItemKind.CRITERION, weight, verdict, model_verdict=None
    ...     )
    >>> score_percent(
    ...     [criterion("must", "met"), criterion("should", "not_met"),
    ...      criterion("may", "met")]
    ... )
    66
    """
    met = total = 0
    for outcome in _criteria_outcomes(items):
        number = WEIGHT_NUMBERS[outcome.weight]
        total += number
        # An unconfirmed criterion counts as met: the model said "done" and
        # the check could not bear it out, which is no evidence the work
        # lacks it — a score that took its weight off would read a fault of
        # the check as the student's. The Mentor session's own choice of
        # 2026-11-07, beside the operator's on the pass (is_passed); undone
        # by dropping ``or outcome.unconfirmed`` here alone.
        if outcome.met or outcome.unconfirmed:
            met += number
    if total == 0:
        msg = "verdicts without a criterion have no score"
        raise ValueError(msg)
    return (100 * met) // total


def is_passed(items: Iterable[ScoredItem]) -> bool:
    """Whether every "must" criterion is met or unconfirmed; with no "must", passed.

    The score shows how complete the work is; it does not decide the pass
    (decision 2). An author's pass mark is outside the first echelon.

    An unconfirmed "must" does not fail the work (the operator's decision of
    2026-11-07): the model said "done", and that no line of the work could
    bear it out is the check's limit, not the student's fault. The author
    sees the flag on its row.
    """
    return all(
        outcome.met or outcome.unconfirmed
        for outcome in _criteria_outcomes(items)
        if outcome.weight is WeightCategory.MUST
    )
