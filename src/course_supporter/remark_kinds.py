"""The closed vocabularies of a remark between attempts (mentor-rebuild task 12a).

Purpose:
    After a review the student may answer a remark — ask, object, comment —
    and the next check answers every reply; between attempts every remark has
    a state. Both are stored (the product owner's decision of 2026-11-16): the
    replies and the answers as rows of ``remark_comments``, the state as a
    column of the verdict row it is computed from
    (``submission_criterion_verdicts.remark_state``). The database refuses a
    word it has not been told about, so the storage layer builds its ``CHECK``
    constraints from these enums — and the storage layer does not import
    ``homework/``. They live here, below both, for that reason, as the
    vocabularies of ``criteria_kinds`` and ``feedback_kinds`` do.

    A reply is a remark comment, not a ``student_feedback`` record: the
    "helped / did not help" touches stay there unchanged.

Interface:
    :class:`RemarkState` — the state of a remark between two attempts.
    :class:`CommentAuthor` — who wrote a comment.
    :class:`CommentKind` — what a comment is.
    :class:`CommentTopic` — whether a student's comment keeps to its remark.
    :data:`COMMENT_MAX_CHARS` — the longest comment the database admits.

Extending:
    A new member is a new enum value AND a migration re-issuing every ``CHECK``
    built from that enum — Postgres cannot widen a CHECK in place, and the
    database refuses a value it has not been told about, so Python and SQL
    cannot drift apart silently. The student's three kinds of
    :class:`CommentKind` are the review's own ``ReplyKind``
    (``models/review_structure.py``); a fourth one is added to both.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class RemarkState(StrEnum):
    """The state of the remark on one item between two attempts (task 12a).

    The item now against the same item in the student's previous completed
    attempt at the same version of the task; kept on the item's verdict row.
    "Credited" is as the score counts it (``homework/criteria_verdicts.py``,
    ``score_percent``): "met", or "not met" with ``quote_not_found`` —
    unconfirmed. That rule lives in the code alone; the database does not
    repeat it. No state (NULL) — credited now, and credited then or nothing to
    compare with.
    """

    NEW = "new"
    """Not credited now; no previous attempt at this version (the first one,
    another version of the task), or the item was not in it."""
    OPEN = "open"
    """Not credited then, not credited now."""
    FIXED = "fixed"
    """Not credited then, credited now."""
    BROKEN = "broken"
    """Credited then, not credited now."""


class CommentAuthor(StrEnum):
    """Who wrote a remark comment."""

    STUDENT = "student"
    MENTOR = "mentor"


class CommentKind(StrEnum):
    """What a remark comment is.

    The student's three are answered alike but named apart — an objection
    answered as if it were a question reads as a brush-off; ``ANSWER`` is the
    Mentor's alone, which the database holds too.
    """

    QUESTION = "question"
    OBJECTION = "objection"
    COMMENT = "comment"
    ANSWER = "answer"


class CommentTopic(StrEnum):
    """Whether a student's comment keeps to the remark it is written on.

    Judged on the student's words alone; the Mentor's comment has no topic.
    """

    ON_TOPIC = "on_topic"
    OFF_TOPIC = "off_topic"
    HOSTILE = "hostile"


COMMENT_MAX_CHARS: Final = 4000
"""The longest comment the database admits, in characters, the Mentor's
answer included. The student's own limit, shorter, is the code's."""
