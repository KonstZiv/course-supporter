"""A test the author writes in the system: its draft, its published form, its digests.

Purpose:
    From task 07b a test is not a file the pipeline reads but an object the
    author writes: questions, options each marked right or wrong, a pass mark
    and, where the author wants them, explanations of their own. Between the
    draft the author edits and the version a student answers sit three
    questions nobody else should answer again: which letter an option gets,
    when two publications are the same test, and what text the explanation
    model reads. They are answered here, by pure functions over frozen models —
    no session, no settings, no clock — so the route that shows a draft, the
    service that publishes it and the worker that explains its key all derive
    the same things from the same body.

Interface:
    :class:`DraftBody` — the draft as it is stored: no numbers, no letters.
    :class:`PublishedBody` — a published version's body: numbered, lettered,
        frozen; its :meth:`~PublishedBody.answer_key` and
        :meth:`~PublishedBody.explanations`.
    :func:`option_letters` — the letters options get, in order, in a language.
    :func:`published_form` — a draft numbered and lettered for publication.
    :func:`version_digests` — the three digests of a published version.
    :func:`render_for_prompt` — the test as the explanation model reads it.

What the models check, and what they do not:
    Types and shape: an unknown field, a string where a boolean belongs, a
    letter in a draft. Not the limits of the format — how many questions and
    options, how long a text. Those are checked where a body enters, with a
    code and a place the author can act on (task 07b, PRE-FLIGHT section 6),
    and a body that reaches this module has passed them. Nor whether a draft is
    finished: from task 07c a draft is saved unfinished — no questions yet, a
    question short of options or of a right one, an empty text — and a check
    and a publication ask :mod:`course_supporter.homework.test_completeness`
    first.
    The one limit enforced here is the letters' own: a question cannot have
    more options than there are letters (:data:`MAX_OPTIONS`).

Letters (task 07b, decision 16):
    The system letters the options, never the author. A course in Ukrainian
    gets the Ukrainian alphabet without ``ґ``, ``й`` and ``ь``; every other
    language gets the small Latin letters. The n-th option gets the n-th letter,
    and questions are numbered from ``1`` in order.

    >>> "".join(option_letters("ukr"))
    'абвгдеєжзиіїклмнопрстуфхцч'
    >>> "".join(option_letters("eng")) == "".join(option_letters("deu"))
    True

    Both sequences meet the canonical label of task 07
    (:func:`~course_supporter.homework.test_text.canonical_label`) without a
    collision, so the door that checks an answer and the path that scores it
    compare labels exactly as they did before a test was an object.

Three digests (task 07b, decision 9):
    A published version has three, each over a different part of it:

    * ``content_digest`` — what the student sees: numbers, texts, letters. It is
      the ``version`` the structure route shows, and the content axis of the
      explanations.
    * ``answers_digest`` — the key, through
      :func:`~course_supporter.homework.reference_key.answers_digest`, the very
      function task 06 digests an author's key with: the answers axis of the
      explanations.
    * ``publication_digest`` — everything published: the two above, the pass
      mark, the author's own explanations and the language. A publication equal
      to the latest version in it creates no version.

    So an edit of the pass mark alone is a new version — a version is never
    rewritten — that the student cannot tell from the last one and that buys
    no new explanations:

    >>> draft = DraftBody(
    ...     pass_threshold=80,
    ...     questions=(
    ...         DraftQuestion(
    ...             text="What does print(2 ** 3) print?",
    ...             options=(
    ...                 DraftOption(text="6", correct=False),
    ...                 DraftOption(text="8", correct=True),
    ...             ),
    ...         ),
    ...     ),
    ... )
    >>> before = version_digests(published_form(draft, "eng"), "eng")
    >>> stricter = draft.model_copy(update={"pass_threshold": 90})
    >>> after = version_digests(published_form(stricter, "eng"), "eng")
    >>> before.content_digest == after.content_digest
    True
    >>> before.answers_digest == after.answers_digest
    True
    >>> before.publication_digest == after.publication_digest
    False

    The test's title is a column of its document and no input of any digest:
    renaming a test is not a new version.

Extending:
    A new kind of question (a number to type in, a match of two lists) is a new
    field on the question models. If the student sees it, it goes into the
    visible digest; if it is part of the key, into the answer key; otherwise
    into the publication digest only. Whichever it is, the golden digests of
    this module's tests move, and that is the point of them: a recipe that
    changed silently would turn the republication of every unchanged draft
    into a new version.
"""

from __future__ import annotations

import hashlib
import json
import string
from typing import Any, Final, NamedTuple

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from course_supporter.homework.reference_key import AnswerKey, answers_digest

__all__ = [
    "MAX_OPTIONS",
    "DraftBody",
    "DraftOption",
    "DraftQuestion",
    "PublishedBody",
    "PublishedOption",
    "PublishedQuestion",
    "VersionDigests",
    "option_letters",
    "published_form",
    "render_for_prompt",
    "version_digests",
]

MAX_OPTIONS: Final[int] = 26
"""The most options a question can have: one letter each (decision 16)."""

_UKRAINIAN_LETTERS: Final[str] = "абвгдеєжзиіїклмнопрстуфхцчшщюя"
"""The Ukrainian alphabet without ``ґ``, ``й`` and ``ь`` — the letters of a
Ukrainian test (decision 16). Thirty; the first :data:`MAX_OPTIONS` are used."""

_LATIN_LETTERS: Final[str] = string.ascii_lowercase
"""The small Latin letters — the letters of a test in any other language."""

_CONTINUATION: Final[str] = "   "
"""What a text's second and later lines start with in the prompt text.

A question or an option may run over several lines — code, most often — and a
line of it that began ``2.`` or ``b)`` would read as a question or an option of
its own. Indented, it cannot.
"""

_MODEL_CONFIG: Final = ConfigDict(extra="forbid", frozen=True)


class DraftOption(BaseModel):
    """An option as the author writes it: its text and whether it is right."""

    model_config = _MODEL_CONFIG

    text: StrictStr
    correct: StrictBool


class DraftQuestion(BaseModel):
    """A question as the author writes it, with an optional explanation."""

    model_config = _MODEL_CONFIG

    text: StrictStr
    options: tuple[DraftOption, ...]
    explanation: StrictStr | None = None


class DraftBody(BaseModel):
    """The draft of a test — the body of ``TestDraft``.

    No numbers and no letters: a question's number is its place in the list
    and an option's letter is its place in the question, and both are set at
    publication. A draft that carried them would let the author write a letter
    the system does not give (decision 16).
    """

    model_config = _MODEL_CONFIG

    pass_threshold: StrictInt | None = None
    questions: tuple[DraftQuestion, ...]

    def to_jsonb(self) -> dict[str, Any]:
        """Serialize for ``TestDraft.body``."""
        return self.model_dump(mode="json")

    @classmethod
    def from_jsonb(cls, payload: dict[str, Any]) -> DraftBody:
        """Deserialize ``TestDraft.body``."""
        return cls.model_validate(payload)


class PublishedOption(BaseModel):
    """An option of a published version: its letter, its text, whether right."""

    model_config = _MODEL_CONFIG

    label: StrictStr
    text: StrictStr
    correct: StrictBool


class PublishedQuestion(BaseModel):
    """A question of a published version: its number, text, options, explanation."""

    model_config = _MODEL_CONFIG

    number: StrictStr
    text: StrictStr
    options: tuple[PublishedOption, ...]
    explanation: StrictStr | None = None


class PublishedBody(BaseModel):
    """A published version of a test — the body of ``TestVersion``, frozen.

    The key is inside it: every option says whether it is right. What of it a
    student may see is decided by the routes that show it, never here.
    """

    model_config = _MODEL_CONFIG

    pass_threshold: StrictInt | None = None
    questions: tuple[PublishedQuestion, ...]

    def answer_key(self) -> AnswerKey:
        """The key: question number → the letters of its right options.

        In the order the options are listed. The form task 06 keeps an author's
        key in, so its digest and the explanation prompt take it as they are.
        """
        return {
            question.number: [
                option.label for option in question.options if option.correct
            ]
            for question in self.questions
        }

    def explanations(self) -> dict[str, str]:
        """The author's own explanations: question number → text, where written."""
        return {
            question.number: question.explanation
            for question in self.questions
            if question.explanation is not None
        }

    def to_jsonb(self) -> dict[str, Any]:
        """Serialize for ``TestVersion.body``."""
        return self.model_dump(mode="json")

    @classmethod
    def from_jsonb(cls, payload: dict[str, Any]) -> PublishedBody:
        """Deserialize ``TestVersion.body``."""
        return cls.model_validate(payload)


class VersionDigests(NamedTuple):
    """The three digests of a published version, named as their columns."""

    content_digest: str
    answers_digest: str
    publication_digest: str


def option_letters(language: str | None) -> tuple[str, ...]:
    """The letters options get, in order, in a course of ``language``.

    Args:
        language: The course language, ISO 639-3. ``None`` — a course with no
            language set — is "any other language".

    Returns:
        :data:`MAX_OPTIONS` letters: Ukrainian for ``ukr``, Latin otherwise.

    >>> option_letters("ukr")[:4] == tuple("абвг")
    True
    >>> option_letters(None)[:4]
    ('a', 'b', 'c', 'd')
    """
    letters = _UKRAINIAN_LETTERS if language == "ukr" else _LATIN_LETTERS
    return tuple(letters[:MAX_OPTIONS])


def published_form(draft: DraftBody, language: str | None) -> PublishedBody:
    """The draft numbered and lettered — what publishing it would freeze.

    Questions are numbered ``1`` … ``N`` in order and the n-th option of each
    gets the n-th letter of :func:`option_letters`. Texts, marks, the pass
    mark and the explanations are carried as they are.

    Args:
        draft: The draft to publish.
        language: The course language the letters are chosen by.

    Returns:
        The published form.

    Raises:
        ValueError: A question has more options than there are letters. A
            checked draft never does; this is the guard, not the check.

    >>> draft = DraftBody(
    ...     questions=(
    ...         DraftQuestion(
    ...             text="2 + 2?",
    ...             options=(
    ...                 DraftOption(text="4", correct=True),
    ...                 DraftOption(text="5", correct=False),
    ...             ),
    ...         ),
    ...     ),
    ... )
    >>> question = published_form(draft, "eng").questions[0]
    >>> question.number, [(o.label, o.correct) for o in question.options]
    ('1', [('a', True), ('b', False)])
    """
    letters = option_letters(language)
    questions: list[PublishedQuestion] = []
    for number, question in enumerate(draft.questions, start=1):
        if len(question.options) > len(letters):
            msg = (
                f"Question {number} has {len(question.options)} options; "
                f"at most {len(letters)} can be lettered."
            )
            raise ValueError(msg)
        questions.append(
            PublishedQuestion(
                number=str(number),
                text=question.text,
                options=tuple(
                    PublishedOption(
                        label=label, text=option.text, correct=option.correct
                    )
                    for label, option in zip(
                        letters[: len(question.options)], question.options, strict=True
                    )
                ),
                explanation=question.explanation,
            )
        )
    return PublishedBody(
        pass_threshold=draft.pass_threshold, questions=tuple(questions)
    )


def version_digests(body: PublishedBody, language: str) -> VersionDigests:
    """The three digests of a published version (decision 9).

    Each is SHA-256 over canonical JSON — keys sorted, lists in order, no
    incidental whitespace, letters unescaped — the recipe of
    ``storage/content_hash.py``, so a digest depends on what the body says and
    never on how a dictionary happened to be ordered.

    Args:
        body: The published form.
        language: The language the version is published in. An input of the
            publication digest only: the letters already carry it into the
            visible one.

    Returns:
        The digests, named as the columns of ``TestVersion``.
    """
    visible = [
        {
            "number": question.number,
            "text": question.text,
            "options": [
                {"label": option.label, "text": option.text}
                for option in question.options
            ],
        }
        for question in body.questions
    ]
    content = _canonical_digest(visible)
    answers = answers_digest(body.answer_key())
    publication = _canonical_digest(
        {
            "content_digest": content,
            "answers_digest": answers,
            "pass_threshold": body.pass_threshold,
            "explanations": body.explanations(),
            "language": language,
        }
    )
    return VersionDigests(
        content_digest=content, answers_digest=answers, publication_digest=publication
    )


def render_for_prompt(body: PublishedBody) -> str:
    """The test as the explanation model reads it (PRE-FLIGHT section 5.3).

    The shape the prompt was written for, the shape an author typed tests in
    before task 07b: a question's line ``N. text``, a line ``letter) text`` for
    each of its options under it, a blank line before the next question. Nothing
    of the key: the right options reach the model separately, as
    :meth:`PublishedBody.answer_key`, and the author's own explanations do not
    reach it at all. A text's second and later lines are indented, so no line
    inside a text can pass for a question or an option.

    >>> draft = DraftBody(
    ...     questions=(
    ...         DraftQuestion(
    ...             text="What does this print?\\nprint(2 ** 3)",
    ...             options=(
    ...                 DraftOption(text="6", correct=False),
    ...                 DraftOption(text="8", correct=True),
    ...             ),
    ...         ),
    ...         DraftQuestion(
    ...             text="Is 1 odd?",
    ...             options=(
    ...                 DraftOption(text="Yes", correct=True),
    ...                 DraftOption(text="No", correct=False),
    ...             ),
    ...         ),
    ...     ),
    ... )
    >>> print(render_for_prompt(published_form(draft, "eng")))
    1. What does this print?
       print(2 ** 3)
    a) 6
    b) 8
    <BLANKLINE>
    2. Is 1 odd?
    a) Yes
    b) No
    """
    return "\n\n".join(
        "\n".join(
            [
                _item(f"{question.number}.", question.text),
                *(
                    _item(f"{option.label})", option.text)
                    for option in question.options
                ),
            ]
        )
        for question in body.questions
    )


def _item(marker: str, text: str) -> str:
    """One question or option: the marker on its first line, the rest indented."""
    first, *rest = text.splitlines() or [""]
    continued = [f"{_CONTINUATION}{line}" if line.strip() else "" for line in rest]
    return "\n".join([f"{marker} {first}", *continued])


def _canonical_digest(payload: object) -> str:
    """SHA-256 hex of ``payload`` as canonical JSON."""
    text = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
