"""The review, as data, before anyone writes it out (mentor-rebuild task 04).

Purpose:
    A review has a fixed shape and a fixed order of sections (``03-BINDING.md``
    §2.13). Holding that shape as data, not as prose, is what lets the same
    review be written in sixty languages, be re-read years later, and be
    checked by a test instead of by reading it.

    This module defines the shape. It does not fill it: the stages of tasks
    08-13 do that, and today every stored review is pre-rebuild, so the field
    is empty on every row in production.

Interface:
    :class:`ReviewStructureV1` — the whole review. Subclass of
    :class:`~course_supporter.models.review_schema.VersionedReview`, so it
    carries that one required ``schema_version`` and declares no version key of
    its own: one review, one version, named in one place.
    :data:`SECTION_ORDER` — the nine sections in the order §2.13 ratified. The
    assembler walks this; the order of fields below is only for reading.
    :class:`Remark`, :class:`Reply`, :class:`Verdict`, :class:`Verification`,
    :class:`Position`, :class:`Reference` — the parts.

Two rules the model enforces rather than documents:

* **Every section is optional, but a review with none is not a review.** A
  student reading "here is your review" followed by nothing is a defect, not an
  edge case, and the place to refuse it is here, where no stage can route
  around it.
* **The language is required.** It is part of the review, not an argument
  passed beside it, so a review cannot be assembled in a language other than
  the one it was written for -- there is no second place to disagree with.

>>> review = ReviewStructureV1(
...     schema_version="1",
...     language="ukr",
...     progress="Третє завдання поспіль без зауважень до стилю.",
... )
>>> review.schema_version, review.language
('1', 'ukr')
>>> ReviewStructureV1(schema_version="1", language="ukr")
Traceback (most recent call last):
    ...
pydantic_core._pydantic_core.ValidationError: ...says nothing to the student...

The examples above are run by ``tests/unit/test_review_structure.py``; this
module's docstrings are checked, not merely written.
"""

from __future__ import annotations

import re
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from course_supporter.models.review_schema import VersionedReview

SECTION_ORDER: Final[tuple[str, ...]] = (
    "verdict",
    "fixed",
    "new_remarks",
    "open",
    "broken",
    "mentor_voice",
    "replies",
    "verification",
    "progress",
)
"""The nine sections, in the order ratified in ``03-BINDING.md`` §2.13.

The order is part of the contract, not a detail of rendering: a student reads
the verdict first and the word about progress last, in every language. It lives
here rather than in the assembler so that "what a review is" and "in what order
it is read" stay in one file, and so a reordering shows up as a change to the
contract.
"""

PositionKind = Literal["video", "slide", "paragraph", "file"]
"""Where in the material a remark points. Each has its own phrase and its own
placeholder: a time for video, a number for a slide or a paragraph, a name for
a file."""

ReplyKind = Literal["question", "objection", "comment"]
"""What the student's remark was. The three are answered alike but named apart:
an objection answered as if it were a question reads as a brush-off."""

_LANGUAGE_CODE = re.compile(r"^[a-z]{3}$")


class Position(BaseModel):
    """Where in the material something is, in the material's own terms.

    Not validated against the material: the structure is written once and read
    later, by which time the material may have moved. A position that no longer
    resolves is a stale reference (§2.12), handled where references are, not
    refused here.

    >>> Position(kind="slide", value="12").kind
    'slide'
    """

    model_config = ConfigDict(extra="forbid")

    kind: PositionKind = Field(description="Which of the four kinds of place.")
    value: str = Field(
        min_length=1,
        description=(
            "The time, number or name itself, already formatted for reading "
            "('03:41', '12', 'main.py'). The assembler puts it into the "
            "phrase's placeholder unchanged."
        ),
    )


class Reference(BaseModel):
    """Something to read, resolved to a link by code before it is stored.

    The model names an identifier from its input; code turns it into this
    (§2.12). By the time a reference is here it has a destination -- which is
    why the assembler never has to decide what to do with a bare id.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, description="What the student will read.")
    url: str = Field(min_length=1, description="Where it is, resolved by code.")


class Remark(BaseModel):
    """One thing to say about the work, in the contrasting form of §2.13.

    Four parts, all required except the reading: what is wrong, why it matters,
    what to do, where to read about it. The form is the point -- a remark that
    says what is wrong without saying why leaves the student to guess whether it
    matters, and one without a ``todo`` leaves them with a verdict instead of a
    lesson.

    >>> Remark(what="Пароль написано просто текстом.",
    ...        why="Його побачить кожен, хто відкриє файл.",
    ...        todo="Перенести пароль до змінної середовища.").read
    []
    """

    model_config = ConfigDict(extra="forbid")

    what: str = Field(min_length=1, description="What is wrong.")
    why: str = Field(min_length=1, description="Why it matters.")
    todo: str = Field(min_length=1, description="What to do about it.")
    read: list[Reference] = Field(
        default_factory=list,
        description="Where to read more. Empty is normal: not every remark needs it.",
    )
    position: Position | None = Field(
        default=None,
        description="Where in the material this is, when the remark has a place.",
    )


class Reply(BaseModel):
    """An answer to something the student said, with what they said kept beside it.

    ``said`` is stored rather than assumed known, so a review read a year later
    still makes sense on its own -- the same reason the language is stored.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ReplyKind = Field(description="Question, objection or comment.")
    said: str = Field(min_length=1, description="What the student wrote.")
    answer: str = Field(min_length=1, description="The Mentor's answer to it.")


class Verdict(BaseModel):
    """Passed or not, and why -- never one without the other.

    ``why`` is required on both outcomes. A pass with no reason teaches as
    little as a failure with none.
    """

    model_config = ConfigDict(extra="forbid")

    passed: bool = Field(description="Whether the work is accepted.")
    why: str = Field(min_length=1, description="The reason, in the student's language.")


class Verification(BaseModel):
    """What was checked by running the work, and what by reading it.

    Two halves of one section, kept apart because the difference is what the
    student is being told: "I ran it and it failed" is a fact, "I read it and it
    looks wrong" is a judgement, and conflating them costs trust.
    """

    model_config = ConfigDict(extra="forbid")

    by_run: list[str] = Field(
        default_factory=list, description="What was established by running it."
    )
    by_reading: list[str] = Field(
        default_factory=list, description="What was established by reading it."
    )

    @model_validator(mode="after")
    def _one_half_at_least(self) -> Self:
        """Both halves empty is the section being absent, not being present.

        Without this, a review whose only section is an empty verification
        passes the whole-review check below and still assembles to nothing.
        """
        if not self.by_run and not self.by_reading:
            msg = "A verification section states what was run or what was read"
            raise ValueError(msg)
        return self


class ReviewStructureV1(VersionedReview):
    """A whole review, in the order it will be read.

    Version comes from :class:`VersionedReview` and is the only one: this class
    adds no version key of its own, so there is never a question of which of two
    versions a reader should believe.

    Every section is ``None`` or empty when it has nothing to say, and the
    assembler leaves silent sections out entirely rather than printing an empty
    heading. What the model refuses is a review with nothing in any of them.
    """

    model_config = ConfigDict(extra="forbid")

    language: str = Field(
        description=(
            "The language this review is written in, ISO 639-3, as "
            "``language.resolve_review_language`` resolved it. Part of the "
            "review because the same structure must give the same text later; "
            "not checked against the allowed list, which may have changed by "
            "the time this is read again."
        ),
    )

    # Declared in SECTION_ORDER's order for reading. The assembler walks
    # SECTION_ORDER, not these fields -- a field moved here changes nothing.
    verdict: Verdict | None = Field(default=None, description="Passed or not, and why.")
    fixed: list[Remark] = Field(
        default_factory=list, description="Remarks from before that are now dealt with."
    )
    new_remarks: list[Remark] = Field(
        default_factory=list, description="What this submission newly gives cause for."
    )
    open: list[Remark] = Field(
        default_factory=list, description="Remarks from before that still stand."
    )
    broken: list[Remark] = Field(
        default_factory=list, description="What used to work and does not now."
    )
    mentor_voice: str | None = Field(
        default=None,
        description="The Mentor speaking as itself, kept in its own block.",
    )
    replies: list[Reply] = Field(
        default_factory=list, description="Answers to what the student wrote."
    )
    verification: Verification | None = Field(
        default=None, description="What was checked by running, and what by reading."
    )
    progress: str | None = Field(
        default=None, description="A word about how the student is doing overall."
    )

    @model_validator(mode="after")
    def _language_is_a_code(self) -> Self:
        if not _LANGUAGE_CODE.match(self.language):
            msg = (
                f"language must be a three-letter ISO 639-3 code, got {self.language!r}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _says_something(self) -> Self:
        if not any(getattr(self, section) for section in SECTION_ORDER):
            msg = (
                "A review with no sections says nothing to the student. Every "
                "section is optional; all of them empty is not a review."
            )
            raise ValueError(msg)
        return self
