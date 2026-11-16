"""Who wrote a comment on a remark, and what the screen made of it (task 12a).

Purpose:
    A student answers a remark of their review — a question, an objection, a
    comment — and the next review answers each reply (product owner's decision,
    2026-11-16). Both live as rows of the comments table (K1); the words that
    stay countable on those rows are named here, beside
    :mod:`course_supporter.feedback_kinds`, for the reason given there: free
    strings would turn the statistics of replies into a text-parsing exercise.

Interface:
    :class:`CommentAuthor` — who wrote the comment.
    :class:`ReplyTopic` — what the reply screen judged a student's reply to be
    (``homework/reply_screen.py``).

    What KIND of reply a student wrote is the review's own vocabulary,
    :data:`~course_supporter.models.review_structure.ReplyKind` (question,
    objection, comment): the review answers each kind by that name, so a
    second list could only disagree with it. Which violation made a reply
    hostile is the safety check's own taxonomy,
    :class:`~course_supporter.security.schemas.ViolationCategory`, for the
    same reason.

Extending:
    A new value is a new member here AND the matching ``CHECK`` of the
    comments table (K1's migration) — the database refuses a value it has not
    been told about, so the two cannot drift silently.
"""

from __future__ import annotations

from enum import StrEnum


class CommentAuthor(StrEnum):
    """Who wrote a comment on a remark.

    * ``STUDENT`` — a reply of the student whose review it is (K3 writes it).
    * ``MENTOR`` — the review's answer to such a reply (K4 writes it).
    """

    STUDENT = "student"
    MENTOR = "mentor"


class ReplyTopic(StrEnum):
    """What a student's reply was judged to be, before any review answers it.

    * ``ON_TOPIC`` / ``OFF_TOPIC`` — about the work or not: the topic port's
      answer (``homework/reply_topic.py``).
    * ``HOSTILE`` — the safety check refused the reply as an attempt on the
      grade or on the check itself. Decided by the safety check alone, never
      by the topic port, and it outranks the topic: the model that answers the
      replies is told the category of a hostile one, never its text (K4).

    A reply not judged yet has no topic at all — not a fourth value — so a
    reply the screen could not judge is told apart from one judged harmless.
    """

    ON_TOPIC = "on_topic"
    OFF_TOPIC = "off_topic"
    HOSTILE = "hostile"
