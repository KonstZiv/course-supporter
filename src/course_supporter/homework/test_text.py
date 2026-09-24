"""A test as structure: its questions and options, and answers to them (task 07).

Purpose:
    A test arrives as text an author wrote and is answered by a student who
    ticks options. Between the two sit three questions nobody else should answer
    again: where the questions and their options are in the text; which labels
    are the same label (``а``, ``А``, `` а) `` and a Latin ``a`` are one answer);
    and what a set of answers looks like written down, so that the same answers
    always give the same bytes. They are answered here, by pure functions — no
    session, no settings, no clock — so the route that shows a test, the door
    that checks an answer, the path that scores it and the digest of the author's
    key all read the same text the same way.

Interface:
    :func:`parse_test` — the questions and options of a test text.
    :func:`canonical_label` — one option label in its canonical form.
    :func:`canonical_answers` — answers with canonical labels, no repeats,
        questions and labels sorted.
    :func:`canonical_answers_json` — those answers as the exact text that is
        hashed (the key's digest) and stored (a submission's answers file).

Which text this reads:
    The task's SOURCE text — its segments joined without a separator
    (:func:`~course_supporter.homework.task_context.load_task_source_text`).
    Not the stitched text the mentor pipeline reads: stitching puts a blank line
    at every segment boundary, and a model chooses the boundaries, in the middle
    of a line as often as not. On the polygon's test all five fell inside the
    text of a question (probe of task 07, section 3).

The format an author writes (ratified 2026-09-19 and 2026-09-23):
    A question is a line that starts with its number and a full stop, ``1.``; an
    option is a line that starts with a one-letter label and a closing
    parenthesis, ``а)``. Both at the very start of the line and typed by hand:
    the automatic list numbering of Word or HTML does not reach the text. Blank
    lines between them are allowed, because a Word paragraph arrives as a block
    of its own. A question's text is its own line and any lines before its
    first option; lines after its last option, up to the next question, are not
    part of the test.

    >>> test = parse_test("1. Що таке тест?\\nа) Перевірка\\nб) Прикраса")
    >>> [(q.number, q.text, [o.label for o in q.options]) for q in test.questions]
    [('1', 'Що таке тест?', ['а', 'б'])]
    >>> parse_test("Вступ.\\n\\n1. Перше?\\n\\nа) так\\n\\n2. Друге?\\nб) ні").numbers()
    ('1', '2')

Canonical labels (ratified 2026-09-23, decision 11):
    One function for every place a label is compared — the author's key, its
    digest, a student's answer and the labels read from the text — so no two of
    them can disagree about what "the same answer" means. Context-free on
    purpose: a canonical label depends on the label alone, never on the text, so
    the digest of an unchanged key never moves because of an edit elsewhere.

    >>> [canonical_label(label) for label in ["а", " А) ", "a", "B.", "в"]]
    ['а', 'а', 'а', 'в', 'в']
    >>> canonical_answers({"2": ["В", "в"], "1": ["b", "а"]})
    {'1': ['b', 'а'], '2': ['в']}

    ``b`` stays ``b``: a lower-case Latin ``b`` resembles no Cyrillic label, and
    a guess would turn a wrong answer into a right one.

Extending:
    A new kind of question (a number to type in, a match of two lists) is a new
    line pattern in :func:`parse_test` and a new field on :class:`Question`; the
    canonical form of answers stays a mapping of question numbers to label sets
    until a kind needs more than that.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

__all__ = [
    "Option",
    "ParsedTest",
    "Question",
    "canonical_answers",
    "canonical_answers_json",
    "canonical_label",
    "parse_test",
]

_QUESTION_LINE: Final[re.Pattern[str]] = re.compile(r"^(\d+)\.(?!\d)[ \t]*(.*)$")
"""A question: the number and a full stop at the very start of the line.

``(?!\\d)`` keeps a decimal out: a line that opens with ``3.14`` is prose, not
question 3.
"""

_OPTION_LINE: Final[re.Pattern[str]] = re.compile(r"^([^\W\d_])\)[ \t]*(.*)$")
"""An option: one letter and a closing parenthesis at the very start of the line."""

_UPPER_TWINS: Final[dict[str, str]] = {
    "A": "А",
    "B": "В",
    "C": "С",
    "E": "Е",
    "H": "Н",
    "I": "І",
    "K": "К",
    "M": "М",
    "O": "О",
    "P": "Р",
    "T": "Т",
    "X": "Х",
    "Y": "У",
}
"""Latin capitals that are drawn exactly like a Cyrillic capital.

Mapped BEFORE the label is lowered: a capital ``B`` is a twin of ``В``, but its
lower case ``b`` is not a twin of ``в``, so lowering first would lose it.
"""

_LOWER_TWINS: Final[dict[str, str]] = {
    "a": "а",
    "c": "с",
    "e": "е",
    "i": "і",
    "o": "о",
    "p": "р",
    "x": "х",
    "y": "у",
}
"""Latin small letters that are drawn exactly like a Cyrillic small letter."""

_LABEL_TAIL: Final[str] = ").\t "
"""What an answer may carry after the label itself: ``а)``, ``а.``, spaces."""


@dataclass(frozen=True, slots=True)
class Option:
    """One option of a question, as the author wrote it."""

    label: str
    text: str

    @property
    def key(self) -> str:
        """The label in the form every comparison uses."""
        return canonical_label(self.label)


@dataclass(frozen=True, slots=True)
class Question:
    """One question: its number, its text and its options in reading order."""

    number: str
    text: str
    options: tuple[Option, ...]


@dataclass(frozen=True, slots=True)
class ParsedTest:
    """The questions a test text yields, in reading order.

    A number that repeats is kept as a second question rather than merged: what
    to do about it is the caller's decision (a door refuses it), and a parser
    that merged would hide the author's mistake from the one who must fix it.
    """

    questions: tuple[Question, ...]

    def numbers(self) -> tuple[str, ...]:
        """The question numbers in reading order, each once."""
        return tuple(dict.fromkeys(question.number for question in self.questions))


def parse_test(text: str) -> ParsedTest:
    """Read the questions and options out of a test's source text.

    Args:
        text: The task's source text, segments joined without a separator
            (see the module docstring for why not the stitched one).

    Returns:
        Every question in reading order with its options. Lines before the first
        question — a title, instructions — are not part of any question.
    """
    questions: list[Question] = []
    number: str | None = None
    text_parts: list[str] = []
    options: list[Option] = []

    def flush() -> None:
        if number is not None:
            questions.append(
                Question(
                    number=number, text=" ".join(text_parts), options=tuple(options)
                )
            )

    for line in text.replace("\r\n", "\n").split("\n"):
        question = _QUESTION_LINE.match(line)
        if question is not None:
            flush()
            number = question.group(1)
            first = question.group(2).strip()
            text_parts = [first] if first else []
            options = []
            continue
        if number is None:
            continue
        option = _OPTION_LINE.match(line)
        if option is not None:
            options.append(Option(label=option.group(1), text=option.group(2).strip()))
            continue
        if not options and line.strip():
            text_parts.append(line.strip())
    flush()
    return ParsedTest(questions=tuple(questions))


def canonical_label(label: str) -> str:
    """One option label, reduced to the form every comparison uses.

    In order: the spaces and the ``)`` or ``.`` an answer may carry after the
    label go; a Latin capital drawn like a Cyrillic one becomes that Cyrillic
    capital; the label is lowered; a Latin small letter drawn like a Cyrillic
    one becomes that Cyrillic letter. On a label that is already canonical —
    one lower-case Cyrillic letter — every step is the identity, which is what
    keeps the digest of every key stored before this function unchanged.

    >>> canonical_label("г")
    'г'
    >>> canonical_label(" E) ")
    'е'
    """
    stripped = label.strip().rstrip(_LABEL_TAIL)
    upper_mapped = "".join(_UPPER_TWINS.get(char, char) for char in stripped)
    lowered = upper_mapped.lower()
    return "".join(_LOWER_TWINS.get(char, char) for char in lowered)


def canonical_answers(answers: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
    """Answers in canonical form: labels canonical, no repeats, all sorted.

    Question numbers lose surrounding spaces and nothing else. A label that is
    empty once canonical (a lone ``)``) is no answer and is dropped; a question
    left with no label stays, with an empty list, because "answered nothing"
    and "did not mention the question" score the same but are not the same
    record.

    Sorting is by string, for questions as for labels. It is the order the
    digest of every stored key was computed in, so it stays.
    """
    merged: dict[str, set[str]] = {}
    for number, labels in answers.items():
        canonical = {canonical_label(label) for label in labels}
        merged.setdefault(number.strip(), set()).update(
            label for label in canonical if label
        )
    return {number: sorted(merged[number]) for number in sorted(merged)}


def canonical_answers_json(answers: Mapping[str, Iterable[str]]) -> str:
    """The canonical answers as the exact text that is hashed and stored.

    Compact separators and unescaped letters: the form the digest of the
    author's key has used since task 06, so a key's digest and a submission's
    answers file are made the same way.

    >>> canonical_answers_json({"2": ["в"], "1": ["Б"]})
    '{"1":["б"],"2":["в"]}'
    """
    return json.dumps(
        canonical_answers(answers), ensure_ascii=False, separators=(",", ":")
    )
