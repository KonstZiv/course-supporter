"""The shape of a test's answer key, and the two things it is checked against.

Purpose:
    A key is only usable if it answers the questions the task actually asks, and
    only cheap if the same key hashes the same way twice. Both are decided here,
    by pure functions over text and dictionaries — no session, no settings, no
    model — so the route, the service and the job all reach the same verdict
    from the same input.

Interface:
    :class:`QuestionNumbers` — what a task text yields: the numbers it asks, and
    whether the text is within the length it may have.
    :func:`parse_question_numbers` — read the numbers out of a task's source text.
    :func:`answers_digest` — the answer-axis version key.
    :func:`compare_to_questions` — what a key is missing and what it invents.

What "question number" means here:
    A question is a line that STARTS with its number and a full stop —
    ``1.``, ``2.`` — as ``TEST-SOURCE.md`` writes them. Not a heading: markdown
    heading marks do not survive ingestion (``ingestion/text.py`` keeps a
    heading's TEXT and drops its ``#``), so a question written as a heading
    would reach this module unnumbered. That is a requirement on the author,
    ratified 2026-09-19, and it is why a text with no numbers at all is a
    distinct outcome rather than an empty success. The line rules themselves
    live in ``homework/test_text.py``: the numbers checked here are the numbers
    of the questions a student is shown, read by the same parser.

    >>> parse_question_numbers("1. Who?\\nа) me\\n\\n2. When?\\nб) now").numbers
    ('1', '2')
    >>> parse_question_numbers("no questions here").numbers
    ()
    >>> parse_question_numbers("see item 1. in the manual").numbers
    ()

Why the digest is canonical:
    JSONB does not promise key order, and neither does a request body, so the
    same key can arrive twice with its questions or its labels shuffled — or
    with ``А`` where it once said ``а``. Hashing what arrived would buy a second
    paid generation for an unchanged key, which is exactly the cost ``TASK.md``
    invariant 4 of task 06 is about. The canonical form is
    :func:`~course_supporter.homework.test_text.canonical_answers_json`, the
    same text a submission's answers are stored as.

    >>> answers_digest({"1": ["б"], "2": ["в"]}) == answers_digest(
    ...     {"2": ["в"], "1": ["б"]}
    ... )
    True
    >>> answers_digest({"1": ["а", "б"]}) == answers_digest({"1": ["б", "а"]})
    True
    >>> answers_digest({"1": ["А"]}) == answers_digest({"1": [" а) "]})
    True
    >>> answers_digest({"1": ["а"]}) == answers_digest({"1": ["б"]})
    False

    The author's own explanations and the pass mark are NOT inputs: editing
    either must not buy a fresh generation of the whole set (ratified 2026-09-19
    and 2026-09-23). They are not parameters of this function, so they cannot be
    passed by accident.

Extending:
    A second kind of reference (task 08's mandatory points) checks itself
    against the same task text but not against question numbers. It gets its own
    comparison function beside :func:`compare_to_questions`; the parsing and the
    digest are reusable as they stand.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import NamedTuple

from course_supporter.homework.task_text import MENTOR_TASK_TEXT_MAX_BYTES
from course_supporter.homework.test_text import canonical_answers_json, parse_test

AnswerKey = dict[str, list[str]]
"""The author's answers: question number → the option labels that are right."""


@dataclass(frozen=True, slots=True)
class QuestionNumbers:
    """What a task text yields: the numbers it asks, and whether it fits.

    ``truncated`` is separate from ``numbers`` because the two failures are
    different answers to the author. An empty ``numbers`` says the text has no
    questions this module can see — the author numbered them some other way. A
    true ``truncated`` says the text is longer than every reader downstream will
    take whole: the mentor pipeline's stitch cuts at the same budget and the
    explanation model reads a bounded text, so a key checked against the whole
    of it would be explained against a part. The name is the refusal's
    (``TASK_TEXT_TRUNCATED``), which the author already reads.
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


def parse_question_numbers(
    task_text: str, *, max_bytes: int = MENTOR_TASK_TEXT_MAX_BYTES
) -> QuestionNumbers:
    """Read the question numbers out of a task's source text.

    Args:
        task_text: The task's segments joined WITHOUT a separator
            (:func:`~course_supporter.homework.task_context.load_task_source_text`).
            Not the stitched text: its boundaries fall mid-line and can split a
            ``12.`` into ``1`` and ``2.`` (probe of task 07, section 3).
        max_bytes: The length, in UTF-8 bytes, beyond which the text counts as
            truncated. The stitch budget by default — the same ceiling every
            reader of the task text works under.

    Returns:
        The numbers in the order they appear, each once, and whether the text is
        over the budget.
    """
    return QuestionNumbers(
        numbers=parse_test(task_text).numbers(),
        truncated=len(task_text.encode("utf-8")) > max_bytes,
    )


def answers_digest(answers: AnswerKey) -> str:
    """SHA-256 over the author's answers in canonical form.

    Canonical means: labels reduced to one form, repeats gone, questions and
    labels sorted, serialized without incidental whitespace
    (:func:`~course_supporter.homework.test_text.canonical_answers_json`). Two
    keys that say the same thing therefore hash the same, however they were
    typed or however the database chose to store them. A key that was already
    canonical hashes exactly as it did before canonical labels existed.

    Args:
        answers: question number → the labels that are right.

    Returns:
        The 64-character hex digest that is the answer axis of a reference
        version's key.
    """
    return hashlib.sha256(canonical_answers_json(answers).encode("utf-8")).hexdigest()


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
