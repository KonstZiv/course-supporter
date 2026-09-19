"""The shape of a test's answer key, and the two things it is checked against.

Purpose:
    A key is only usable if it answers the questions the task actually asks, and
    only cheap if the same key hashes the same way twice. Both are decided here,
    by pure functions over text and dictionaries — no session, no settings, no
    model — so the route, the service and the job all reach the same verdict
    from the same input.

Interface:
    :class:`QuestionNumbers` — what a task text yields: the numbers it asks, and
    whether the text reached the reader whole.
    :func:`parse_question_numbers` — read the numbers out of a stitched task text.
    :func:`answers_digest` — the answer-axis version key.
    :func:`compare_to_questions` — what a key is missing and what it invents.

What "question number" means here:
    A question is a line that STARTS with its number and a full stop —
    ``1.``, ``2.`` — as ``TEST-SOURCE.md`` writes them. Not a heading: markdown
    heading marks do not survive ingestion (``ingestion/text.py`` keeps a
    heading's TEXT and drops its ``#``), so a question written as a heading
    would reach this module unnumbered. That is a requirement on the author,
    ratified 2026-09-19, and it is why a text with no numbers at all is a
    distinct outcome rather than an empty success.

    >>> parse_question_numbers("1. Who?\\nа) me\\n\\n2. When?\\nб) now").numbers
    ('1', '2')
    >>> parse_question_numbers("no questions here").numbers
    ()
    >>> parse_question_numbers("see item 1. in the manual").numbers
    ()

Why the digest is canonical:
    JSONB does not promise key order, and neither does a request body, so the
    same key can arrive twice with its questions or its labels shuffled. Hashing
    what arrived would buy a second paid generation for an unchanged key, which
    is exactly the cost ``TASK.md`` invariant 4 is about.

    >>> answers_digest({"1": ["б"], "2": ["в"]}) == answers_digest(
    ...     {"2": ["в"], "1": ["б"]}
    ... )
    True
    >>> answers_digest({"1": ["а", "б"]}) == answers_digest({"1": ["б", "а"]})
    True
    >>> answers_digest({"1": ["а"]}) == answers_digest({"1": ["б"]})
    False

    The author's own explanations are NOT an input: editing one must not buy a
    fresh generation of the whole set (ratified 2026-09-19). They are not a
    parameter of this function, so they cannot be passed by accident.

Extending:
    A second kind of reference (task 08's mandatory points) checks itself
    against the same task text but not against question numbers. It gets its own
    comparison function beside :func:`compare_to_questions`; the parsing and the
    digest are reusable as they stand.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final, NamedTuple

AnswerKey = dict[str, list[str]]
"""The author's answers: question number → the option labels that are right."""

_QUESTION_LINE: Final[re.Pattern[str]] = re.compile(r"^(\d+)\.", re.MULTILINE)
"""A question is a line that STARTS with its number and a full stop.

Anchored at the start of a line, so a number inside a sentence is not a
question. ``re.MULTILINE`` is what makes the anchor mean "start of any line" —
the stitched task text is one string with blank lines in it, not a list.
"""

_TRUNCATION_MARKER: Final[str] = "[TASK_TEXT TRUNCATED:"
"""Opening of the marker ``task_text.stitch_task_text`` appends when it drops segments.

Only the opening, because the rest of that line carries counts that change per
call. The module does not import the private template it comes from; a test
builds a genuinely over-budget text through ``stitch_task_text`` and asserts
this detector catches it, so a change on either side surfaces as a red test
rather than as a key silently accepted against half a task.
"""


@dataclass(frozen=True, slots=True)
class QuestionNumbers:
    """What a task text yields: the numbers it asks, and whether it arrived whole.

    ``truncated`` is separate from ``numbers`` because the two failures are
    different answers to the author. An empty ``numbers`` says the text has no
    questions this module can see — the author numbered them some other way. A
    true ``truncated`` says the text was cut before it got here, so the numbers
    found are a PREFIX of the real set and a key checked against them would be
    rejected for questions it cannot know about.
    """

    numbers: tuple[str, ...]
    truncated: bool

    def __bool__(self) -> bool:
        """True when the text yielded usable questions."""
        return bool(self.numbers) and not self.truncated


class KeyMismatch(NamedTuple):
    """What a key is missing, and what it invents.

    Both halves are reported together on purpose: an author who renumbered a
    question sees one number missing and one unknown, and two separate refusals
    would make that look like two unrelated mistakes.
    """

    missing: tuple[str, ...]
    unknown: tuple[str, ...]

    def __bool__(self) -> bool:
        """True when the key does not match the questions."""
        return bool(self.missing or self.unknown)


def parse_question_numbers(task_text: str) -> QuestionNumbers:
    """Read the question numbers out of a stitched task text.

    Args:
        task_text: The task as the mentor pipeline assembles it — segment
            contents joined with blank lines
            (:func:`~course_supporter.homework.task_text.stitch_task_text`).

    Returns:
        The numbers in the order they appear, de-duplicated, and whether the
        text carried the truncation marker.

    The joiner between segments is why this reads lines rather than splitting
    the text: stitching inserts a blank line at every segment boundary, so a
    question that opened a segment gains an empty line before it and nothing
    else. A line-anchored pattern does not notice; an index-based one would.
    """
    normalized = task_text.replace("\r\n", "\n")
    seen: dict[str, None] = {}
    for match in _QUESTION_LINE.finditer(normalized):
        seen.setdefault(match.group(1), None)
    return QuestionNumbers(
        numbers=tuple(seen),
        truncated=_TRUNCATION_MARKER in normalized,
    )


def answers_digest(answers: AnswerKey) -> str:
    """SHA-256 over the author's answers in canonical form.

    Canonical means: questions sorted, labels within each question sorted, and
    the whole thing serialized without incidental whitespace. Two keys that say
    the same thing therefore hash the same, however they were typed or however
    the database chose to store them.

    Args:
        answers: question number → the labels that are right.

    Returns:
        The 64-character hex digest that is the answer axis of a reference
        version's key.
    """
    canonical = {
        question: sorted(labels) for question, labels in sorted(answers.items())
    }
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compare_to_questions(answers: AnswerKey, questions: QuestionNumbers) -> KeyMismatch:
    """Which questions the key leaves unanswered, and which it answers in vain.

    Args:
        answers: the author's key.
        questions: what :func:`parse_question_numbers` found in the task text.

    Returns:
        The two differences, each sorted numerically so the author reads them in
        the order the questions appear rather than as strings (``10`` after
        ``9``, not before it).

    >>> numbers = parse_question_numbers("1. a\\n2. b\\n3. c")
    >>> compare_to_questions({"1": ["а"], "2": ["б"], "3": ["в"]}, numbers)
    KeyMismatch(missing=(), unknown=())
    >>> compare_to_questions({"1": ["а"], "4": ["г"]}, numbers)
    KeyMismatch(missing=('2', '3'), unknown=('4',))
    """
    asked = set(questions.numbers)
    answered = set(answers)
    return KeyMismatch(
        missing=_in_question_order(asked - answered),
        unknown=_in_question_order(answered - asked),
    )


def _in_question_order(numbers: set[str]) -> tuple[str, ...]:
    """Sort question numbers the way a reader counts them, not the way bytes sort.

    A non-numeric label is possible only from a malformed key, and it sorts
    after the numbers rather than raising: this function describes a mismatch
    that is already being refused, and it must not fail while doing so.
    """
    return tuple(
        sorted(numbers, key=lambda n: (0, int(n), "") if n.isdigit() else (1, 0, n))
    )
