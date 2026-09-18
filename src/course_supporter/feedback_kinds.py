"""What a student can say about what they were shown (mentor-rebuild task 05).

Purpose:
    A feedback record names three things that have to stay countable: WHAT it
    points at, WHICH KIND of feedback it is, and WHAT was said. Free strings
    would turn the first statistic we ever collect about reviews into a
    text-parsing exercise — the mistake ``error_message`` cost the call
    register (``DD-SP-AC``).

Interface:
    :class:`FeedbackTargetKind` — what the record points at.
    :class:`FeedbackKind` — the kind of feedback the record is.
    :class:`FeedbackValue` — the answer itself.

Extending:
    A new value is a new enum member AND a migration re-issuing the matching
    ``CHECK`` on ``student_feedback`` (Postgres cannot widen a CHECK in place).
    The database refuses a value it has not been told about, so the two cannot
    drift silently. The second echelon — a touch on a remark, a touch on a
    library link, a reply in text — arrives as MEMBERS of these three enums,
    not as new routes and not as new tables (``03-BINDING.md`` §2.14).

    One of those members costs more than a CHECK, and the price is named here
    rather than discovered later. A reply is a record of a conversation branch
    (§2.14), so a target can carry SEVERAL of them from the same student, and
    a reply carries text where a touch carries an answer. Adding it therefore
    changes two more rules of the table in the same migration: the uniqueness
    narrows to touches and becomes partial, and ``value`` becomes optional.
    Neither is built ahead of time: today every row is a touch, and for touches
    the plain uniqueness the table has is the correct one.
"""

from __future__ import annotations

from enum import StrEnum


class FeedbackTargetKind(StrEnum):
    """What a feedback record points at.

    * ``REVIEW`` — the review of one submission. The only target of the first
      echelon: the review is what the student reads, so it is the first thing
      worth asking about.
    """

    REVIEW = "review"


class FeedbackKind(StrEnum):
    """The kind of feedback a record carries.

    * ``TOUCH`` — one tap, no text. The cheapest thing a student can say, and
      the only kind that fits between reading a review and closing the tab.
    """

    TOUCH = "touch"


class FeedbackValue(StrEnum):
    """The answer itself.

    * ``HELPED`` / ``NOT_HELPED`` — the two answers a touch on a review can
      carry.

    A closed list of strings rather than a boolean on purpose: a boolean has
    nowhere to put a third answer, and "helped = false" reads in SQL as an
    absence rather than as an answer given.
    """

    HELPED = "helped"
    NOT_HELPED = "not_helped"
