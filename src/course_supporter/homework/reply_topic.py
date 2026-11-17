"""Is a student's reply about the work? — the port and its placeholder (task 12a, K3).

Purpose:
    A reply to a remark is answered by the next review; whether it is about
    the work or about something else decides how (K4). The decision is a
    model's — the decision model chosen by the measurement of 2026-11-17 (Jev,
    Gemini Decision 1 or OpenAI) — and until that measurement it is nobody's:
    this module is the port the reply screen asks, and a placeholder adapter
    that answers the same for every reply.

    Whether a reply is HOSTILE — an attempt on the grade or on the check — is
    not this port's question: the safety check decides it
    (``homework/reply_screen.py``), and a reply it refused never reaches this
    port, so no model reads a text the safety check turned down.

Interface:
    :class:`TopicVerdict` — ``on_topic`` or ``off_topic``, and the model's
    confidence (``None`` when it gives none).
    :class:`ReplyTopicClassifier` — the port (a structural ``Protocol``):
    ``classify(reply, submission=...)``.
    :class:`EverythingOnTopic` — the placeholder: every reply ``on_topic``,
    confidence ``None`` — "no model judged it", not "judged with certainty".
    :func:`build_reply_topic_classifier` — the one place the adapter is
    chosen; tests of other modules substitute it.

Replacing:
    The adapter of the decision model implements ``classify``: it reads what
    it needs through the session it is built on — the remark the reply
    answers (``reply.submission_id`` + ``reply.item_id``: the verdict row),
    the task — and calls its model through the router under an action of its
    own (``reply_topic``, say), so the price of the topics stays apart from
    the price of the safety check (``reply_safety``). Its failure to answer
    is ``LadderExhaustedError``: the screen leaves the reply unjudged. Return
    it from :func:`build_reply_topic_classifier`; nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from course_supporter.remark_kinds import CommentTopic

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.remark_comments import CommentRecord
    from course_supporter.storage.orm import HomeworkSubmission

__all__ = [
    "EverythingOnTopic",
    "ReplyTopicClassifier",
    "TopicVerdict",
    "build_reply_topic_classifier",
]


@dataclass(frozen=True, slots=True)
class TopicVerdict:
    """The port's answer: about the work or not, and how sure.

    Raises:
        ValueError: on ``HOSTILE`` — the safety check's word, never a topic.
    """

    topic: CommentTopic
    confidence: float | None

    def __post_init__(self) -> None:
        if self.topic is CommentTopic.HOSTILE:
            msg = "hostile is the safety check's verdict, not a topic"
            raise ValueError(msg)
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence {self.confidence} is outside 0..1"
            raise ValueError(msg)


class ReplyTopicClassifier(Protocol):
    """The port; the module docstring states the contract."""

    async def classify(
        self, reply: CommentRecord, *, submission: HomeworkSubmission
    ) -> TopicVerdict:
        """Is ``reply`` about the work? ``submission`` is the one answering it."""
        ...


class EverythingOnTopic:
    """The placeholder until the measurement of 2026-11-17 chooses a model.

    Every reply is about the work, and no confidence is claimed: the screen
    records ``on_topic`` with an empty confidence, which reads as "judged by
    no model" in every count made from the comments.
    """

    async def classify(
        self, reply: CommentRecord, *, submission: HomeworkSubmission
    ) -> TopicVerdict:
        return TopicVerdict(topic=CommentTopic.ON_TOPIC, confidence=None)


def build_reply_topic_classifier(session: AsyncSession) -> ReplyTopicClassifier:
    """The production adapter; the one place it is chosen.

    The placeholder today. The decision model's adapter is built on
    ``session`` (module docstring, "Replacing").
    """
    return EverythingOnTopic()
