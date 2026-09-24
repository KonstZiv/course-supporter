"""The one place a review becomes text (mentor-rebuild task 04, block B2).

Purpose:
    A review is data (:class:`ReviewStructureV1`); a student reads words. This
    turns the first into the second, in the student's language, with nothing
    written by a model: the model supplies the remarks and the answers, every
    heading, label and service phrase comes from the phrasebook.

    Before this, a review's text came out of a synthesis call — the second most
    expensive call of a submission. Assembling it here costs nothing and gives
    the same words in sixty languages.

Interface:
    :func:`assemble_review` — structure to markdown. The single producer of
    new-path review markdown; anywhere else that builds review text is a bug,
    not a second implementation.

Three properties this file is built around:

* **Pure.** No database, no network, no clock, no randomness. The same
  structure gives the same bytes, today and in a year, which is what makes the
  snapshot tests a proof rather than a photograph.
* **The language comes from the structure**, never from an argument. There is
  no way to ask for a review in a language other than the one it was written
  for — see :class:`ReviewStructureV1`.
* **No fallback of its own.** Phrases are read through
  :func:`~course_supporter.phrasebook.phrases_for`, the same cached reader
  everything else uses, and a key that is somehow missing raises. The startup
  check already refuses a phrasebook with a missing key, so this cannot happen
  in a booted system; if it somehow did, failing is right and quietly
  substituting English is not. A review half in the student's language, with no
  error anywhere, is the worst of the available outcomes.

>>> from course_supporter.models.review_structure import Verdict
>>> review = ReviewStructureV1(
...     schema_version="1",
...     language="eng",
...     verdict=Verdict(passed=True, why="Everything asked for is there."),
... )
>>> print(assemble_review(review))
# Review
<BLANKLINE>
## Passed
<BLANKLINE>
Everything asked for is there.
<BLANKLINE>
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from course_supporter.models.review_structure import (
    SECTION_ORDER,
    Position,
    Remark,
    Reply,
    ReviewStructureV1,
    TestOption,
    TestSection,
    Verification,
)
from course_supporter.phrasebook import phrases_for

__all__ = ["assemble_review"]


class MissingPhraseError(KeyError):
    """A phrase the assembler needs is not in the language's file.

    Unreachable in a booted system: the startup check refuses a phrasebook
    whose languages do not all carry every key of the source. It exists so
    that if the impossible happens the review stops, loudly, instead of
    arriving with an English heading in the middle of a Persian page.
    """


def _phrase(phrases: Mapping[str, str], key: str, language: str) -> str:
    try:
        return phrases[key]
    except KeyError as exc:
        msg = f"Phrase {key!r} is missing for language {language!r}"
        raise MissingPhraseError(msg) from exc


def _fill(text: str, value: str) -> str:
    """Put ``value`` into the phrase's placeholder, wherever the language put it.

    Every position phrase has exactly one placeholder, and a translation is
    refused at startup unless it carries the same ones as the source, so
    replacing each placeholder with the same value is exact rather than
    approximate. The name is not looked up: a language may write ``{number}``
    before or after the word, and neither this nor the caller needs to know
    which.
    """
    out: list[str] = []
    rest = text
    while "{" in rest and "}" in rest[rest.index("{") :]:
        start = rest.index("{")
        end = rest.index("}", start)
        out.append(rest[:start])
        out.append(value)
        rest = rest[end + 1 :]
    out.append(rest)
    return "".join(out)


def _position_line(
    position: Position, phrases: Mapping[str, str], language: str
) -> str:
    """Where the remark points, as its own italic line above the remark."""
    phrase = _phrase(phrases, f"position.{position.kind}", language)
    return f"*{_fill(phrase, position.value)}*"


def _remark_block(
    remark: Remark, phrases: Mapping[str, str], language: str
) -> list[str]:
    """One remark in the contrasting form: what, why, what to do, what to read.

    The labels come from the phrasebook; the colon after each is punctuation
    the assembler adds, the same in every language. A language that needs
    different punctuation there would need it as part of the phrase, which is a
    change to the dictionary, not to this file.

    The parts are list items, not consecutive lines. Markdown joins consecutive
    lines into one paragraph, which would run all four parts of the remark
    together; the alternative of ending each line with two spaces is worse,
    because the whitespace hook strips them and the damage is invisible in the
    diff that causes it.
    """
    lines: list[str] = []
    if remark.position is not None:
        lines.append(_position_line(remark.position, phrases, language))
        lines.append("")
    for key, value in (
        ("remark.what", remark.what),
        ("remark.why", remark.why),
        ("remark.todo", remark.todo),
    ):
        lines.append(f"- **{_phrase(phrases, key, language)}:** {value}")
    if remark.read:
        links = ", ".join(f"[{ref.title}]({ref.url})" for ref in remark.read)
        lines.append(f"- **{_phrase(phrases, 'remark.read', language)}:** {links}")
    return lines


def _remarks_section(
    remarks: Sequence[Remark], phrases: Mapping[str, str], language: str
) -> list[str]:
    """Several remarks, each its own block, separated by a blank line."""
    blocks: list[list[str]] = [
        _remark_block(remark, phrases, language) for remark in remarks
    ]
    lines: list[str] = []
    for index, block in enumerate(blocks):
        if index:
            lines.append("")
        lines.extend(block)
    return lines


def _replies_section(
    replies: Sequence[Reply], phrases: Mapping[str, str], language: str
) -> list[str]:
    """What the student said and what the Mentor answered, kept together.

    The three kinds are named apart because an objection answered as if it were
    a question reads as a brush-off.
    """
    lines: list[str] = []
    answer_label = _phrase(phrases, "reply.answer", language)
    for index, reply in enumerate(replies):
        if index:
            lines.append("")
        kind_label = _phrase(phrases, f"reply.kind.{reply.kind}", language)
        lines.append(f"- **{kind_label}:** {reply.said}")
        lines.append(f"- **{answer_label}:** {reply.answer}")
    return lines


def _verification_section(
    verification: Verification, phrases: Mapping[str, str], language: str
) -> list[str]:
    """The two halves, each with its own label, each a list.

    Kept apart on purpose: "I ran it and it failed" is a fact and "I read it
    and it looks wrong" is a judgement, and merging them costs the student the
    difference.
    """
    lines: list[str] = []
    for key, items in (
        ("verification.by_run", verification.by_run),
        ("verification.by_reading", verification.by_reading),
    ):
        if not items:
            continue
        if lines:
            lines.append("")
        lines.append(f"**{_phrase(phrases, key, language)}:**")
        lines.append("")
        lines.extend(f"- {item}" for item in items)
    return lines


def _score_line(test: TestSection, phrases: Mapping[str, str], language: str) -> str:
    """The test's score, as the phrase with the number put in."""
    return _fill(_phrase(phrases, "test.score", language), str(test.score))


def _option(option: TestOption) -> str:
    """An option as the student saw it in the test: its label, then its text."""
    return f"{option.label}) {option.text}".rstrip()


def _test_section(
    test: TestSection,
    phrases: Mapping[str, str],
    language: str,
    *,
    with_score: bool,
) -> list[str]:
    """The questions of a test, one by one, in the order they were asked.

    A right answer is its verdict and nothing else. A wrong one adds the
    correct option — label and text, what the student saw — and its
    explanation, or the phrase that there is none; that phrase promises
    nothing, because an explanation may never come (``03-BINDING.md`` 4.4,
    point 8).

    The score opens the section only when the review has no verdict to carry
    it (``with_score``). When some explanation is in the course language rather
    than the review's, one line says so before the questions. The offer to
    take the test again is the section's last line — and, a test review having
    no other sections, the review's last line too (task 07, decision 13).
    """
    lines: list[str] = []
    if with_score:
        lines.append(_score_line(test, phrases, language))
    if test.explanations_in_course_language:
        if lines:
            lines.append("")
        lines.append(_phrase(phrases, "test.explanations_in_course_language", language))

    answer_label = _phrase(phrases, "test.correct_answer", language)
    for question in test.questions:
        if lines:
            lines.append("")
        number = _fill(_phrase(phrases, "test.question", language), question.number)
        key = "test.correct" if question.correct else "test.incorrect"
        lines.append(f"**{number}:** {_phrase(phrases, key, language)}")
        if question.correct:
            continue
        answer = "; ".join(_option(option) for option in question.correct_answer or ())
        explanation = question.explanation or _phrase(
            phrases, "test.no_explanation", language
        )
        lines.extend(("", f"- **{answer_label}:** {answer}", f"- {explanation}"))

    if test.retry_offer:
        lines.extend(("", _phrase(phrases, "test.try_again", language)))
    return lines


def assemble_review(structure: ReviewStructureV1) -> str:
    """Write the review out as markdown, in the language it was written for.

    Sections come in the order :data:`SECTION_ORDER` ratified (§2.13), and a
    section with nothing in it is left out entirely rather than printed as an
    empty heading. The structure itself refuses to be empty, so the result is
    never just a title.

    Args:
        structure: The review. Its ``language`` decides the words; there is no
            second argument that could disagree with it.

    Returns:
        Markdown, ending in a single newline.

    Raises:
        MissingPhraseError: a phrase the review needs is absent from the
            language's file — impossible past the startup check, and a
            deliberate failure rather than a silent substitution.
    """
    phrases = phrases_for(structure.language)
    language = structure.language
    lines: list[str] = [f"# {_phrase(phrases, 'review.title', language)}"]

    for section in SECTION_ORDER:
        rendered = _section(structure, section, phrases, language)
        if rendered is None:
            continue
        heading, body = rendered
        lines.extend(("", f"## {heading}", ""))
        lines.extend(body)

    return "\n".join(lines) + "\n"


def _section(
    structure: ReviewStructureV1,
    section: str,
    phrases: Mapping[str, str],
    language: str,
) -> tuple[str, list[str]] | None:
    """One section's heading and body, or ``None`` when it has nothing to say.

    Written as a chain over named attributes rather than a lookup on ``section``
    so every branch is typed: a section renamed in the structure stops this from
    type-checking instead of silently rendering nothing.
    """
    if section == "verdict":
        verdict = structure.verdict
        if verdict is None:
            return None
        key = "verdict.passed" if verdict.passed else "verdict.failed"
        # A test's score stands right under the verdict it decided; a verdict
        # without a test always says why (the structure refuses otherwise).
        body: list[str] = []
        if structure.test is not None:
            body.append(_score_line(structure.test, phrases, language))
        if verdict.why is not None:
            body.extend(("", verdict.why) if body else (verdict.why,))
        return _phrase(phrases, key, language), body

    def heading() -> str:
        return _phrase(phrases, f"section.{section}", language)

    if section == "test":
        test = structure.test
        if test is None:
            return None
        return heading(), _test_section(
            test, phrases, language, with_score=structure.verdict is None
        )

    if section in {"fixed", "new_remarks", "open", "broken"}:
        remarks: Sequence[Remark] = getattr(structure, section)
        if not remarks:
            return None
        return heading(), _remarks_section(remarks, phrases, language)

    if section == "replies":
        if not structure.replies:
            return None
        return heading(), _replies_section(structure.replies, phrases, language)

    if section == "verification":
        verification = structure.verification
        if verification is None:
            return None
        return heading(), _verification_section(verification, phrases, language)

    if section == "mentor_voice":
        if structure.mentor_voice is None:
            return None
        return heading(), [structure.mentor_voice]

    if section == "progress":
        if structure.progress is None:
            return None
        return heading(), [structure.progress]

    msg = (
        f"No rendering for section {section!r}: SECTION_ORDER names a section "
        f"this file does not write"
    )
    raise NotImplementedError(msg)
