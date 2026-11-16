"""The comments on a review's remarks, as the rest of the code sees them (task 12a, K3).

Purpose:
    A student replies to a remark of their review; the next review answers the
    reply (product owner's decision, 2026-11-16). Every such comment is a row
    of the comments table, which is K1's. This module is what the reply core,
    the reply screen, the submission's state and the rule of the same work
    depend on instead of that table: the values a comment is read as, the
    port that stores and reads them, and the ONE definition of "the replies
    the next review has to answer" — so the four cannot come to mean four
    different sets of replies.

Interface:
    :class:`CommentRecord` — one comment, as the store hands it back.
    :class:`NewReply` — a student's reply, as the core hands it in.
    :class:`ScreenMark` — what the reply screen judged a reply to be.
    :class:`ThreadScope` — the submissions a thread spans: one student, one
    task, one version of it.
    :class:`RemarkCommentRepository` — the port (a structural ``Protocol``):
    six methods, the contract of each in its docstring.
    :func:`build_remark_comment_repository` — the one place the store is
    chosen; tests of other modules substitute it.
    :func:`pending_replies` — the replies the submission is to answer.

What "the replies to answer" are:
    The student's replies that no review has answered yet
    (``answered_in_submission_id`` empty) to the remarks of their PREVIOUS
    ATTEMPT — the latest live submission of theirs on the task that got a
    review, the attempt ledger's own notion
    (:meth:`~course_supporter.storage.homework_repository.HomeworkRepository.attempts_before`)
    — when that attempt was read against the version of the task in force
    now. Replies to a review of an earlier version are not answered: after
    the author changed the task, its criteria — and so the remarks — may be
    other ones under the same ids. A type that takes no replies
    (:func:`~course_supporter.homework.path_config.replies_enabled`) has none,
    and nothing is asked of the store.

Phase 1 of K3:
    The comments table is K1's and is not merged yet, so the store chosen
    here answers every call with :class:`CommentsStoreUnavailableError`. That is
    safe because every reader asks the switch first and the switch is off
    for every type (``config/submission_paths.yaml``); phase 2 returns the
    store over K1's table from :func:`build_remark_comment_repository`, and
    nothing else changes.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select

from course_supporter.homework.path_config import replies_enabled
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import AuthoredDocument

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.path_config import PathConfig
    from course_supporter.models.review_structure import ReplyKind
    from course_supporter.models.source import AssignmentType
    from course_supporter.reply_kinds import CommentAuthor, ReplyTopic
    from course_supporter.security.schemas import ViolationCategory

__all__ = [
    "CommentRecord",
    "CommentsStoreUnavailableError",
    "NewReply",
    "RemarkCommentRepository",
    "ScreenMark",
    "ThreadScope",
    "build_remark_comment_repository",
    "current_task_version",
    "pending_replies",
]


@dataclass(frozen=True, slots=True)
class CommentRecord:
    """One comment on one remark, as the store hands it back.

    Attributes:
        id: The comment.
        submission_id: The submission whose review the comment is attached to.
        item_id: The remark: ``c3`` — a criterion, ``c3.p2`` — a point of it.
        parent_id: The comment of the same thread this one answers; ``None``
            for a reply to the remark itself.
        author: Who wrote it.
        kind: For a student's reply, the review's own vocabulary (question,
            objection, comment); a Mentor's answer carries the kind K4 gives
            it.
        body: The text, as it was stored.
        created_at: When it was written; a thread reads in this order.
        answered_in_submission_id: For a student's reply, the submission whose
            review answered it; ``None`` while no review has.
        topic: What the reply screen judged the reply to be; ``None`` until it
            judged it — and for a reply it could not judge.
        topic_confidence: The confidence of the verdict that set ``topic``:
            the topic port's, or the safety check's for a hostile reply.
            ``None`` when the verdict carried none.
        hostile_category: The safety check's violation behind ``HOSTILE``;
            ``None`` otherwise.
    """

    id: uuid.UUID
    submission_id: uuid.UUID
    item_id: str
    parent_id: uuid.UUID | None
    author: CommentAuthor
    kind: str
    body: str
    created_at: datetime
    answered_in_submission_id: uuid.UUID | None = None
    topic: ReplyTopic | None = None
    topic_confidence: float | None = None
    hostile_category: ViolationCategory | None = None


@dataclass(frozen=True, slots=True)
class NewReply:
    """A student's reply as the core hands it to the store: checked, and final.

    ``tenant_id`` is the submission's, read off the row the core checked —
    never a value the request carried beside it.
    """

    tenant_id: uuid.UUID
    submission_id: uuid.UUID
    item_id: str
    parent_id: uuid.UUID | None
    kind: ReplyKind
    body: str


@dataclass(frozen=True, slots=True)
class ScreenMark:
    """What the reply screen decided about one reply (``ReplyTopic`` and why)."""

    topic: ReplyTopic
    topic_confidence: float | None
    hostile_category: ViolationCategory | None


@dataclass(frozen=True, slots=True)
class ThreadScope:
    """The submissions one thread spans: one student, one task, one version.

    A thread of a remark runs through every attempt the student made at the
    same version of the task — a reply on one attempt, its answer in the next
    review, the student's answer to that — and stops at a new version: the
    remark ``c3`` of another version may be another criterion. ``task_version``
    is compared as it is stored, an absent version matching only an absent
    one; the store keeps to live submissions of ``tenant_id``.
    """

    tenant_id: uuid.UUID
    student_id: uuid.UUID
    authored_document_id: uuid.UUID
    task_version: str | None


class RemarkCommentRepository(Protocol):
    """The store of comments on remarks — the port of this module.

    Writes ``flush`` and never commit: the caller owns the transaction (the
    route commits the reply, the body of the path commits the screen's marks
    with what it writes next).
    """

    async def lock_replies(self, submission_id: uuid.UUID) -> None:
        """Hold back every other writer of this submission's replies.

        Until the caller's transaction ends. The limit of replies per remark
        is a count followed by an insert; two requests that counted before
        either inserted would both pass it, so the core takes this before it
        counts.
        """
        ...

    async def count_student_replies(
        self, *, submission_id: uuid.UUID, item_id: str
    ) -> int:
        """How many replies the student wrote on this remark of this review."""
        ...

    async def add_student_reply(self, reply: NewReply) -> CommentRecord:
        """Store a student's reply, unanswered and not yet judged."""
        ...

    async def thread(
        self, scope: ThreadScope, item_ids: Collection[str]
    ) -> list[CommentRecord]:
        """Every comment of the scope's submissions on these remarks.

        Both authors, oldest first (by ``created_at``, then by ``id``).
        """
        ...

    async def unanswered_student_replies(
        self, submission_id: uuid.UUID
    ) -> list[CommentRecord]:
        """The student's replies on this submission no review has answered.

        Oldest first, judged or not.
        """
        ...

    async def mark_screened(self, comment_id: uuid.UUID, mark: ScreenMark) -> None:
        """Record what the reply screen decided about one reply."""
        ...


class CommentsStoreUnavailableError(RuntimeError):
    """Phase 1 of K3: there is no store of comments until K1's table is in."""

    def __init__(self) -> None:
        super().__init__(
            "No store of remark comments: the comments table (task 12a, K1) is "
            "not merged yet. Keep replies_enabled off for every type "
            "(config/submission_paths.yaml) until K3's phase 2 is."
        )


class _NoCommentsStore:
    """The store of phase 1: every call is refused (module docstring)."""

    async def lock_replies(self, submission_id: uuid.UUID) -> None:
        raise CommentsStoreUnavailableError

    async def count_student_replies(
        self, *, submission_id: uuid.UUID, item_id: str
    ) -> int:
        raise CommentsStoreUnavailableError

    async def add_student_reply(self, reply: NewReply) -> CommentRecord:
        raise CommentsStoreUnavailableError

    async def thread(
        self, scope: ThreadScope, item_ids: Collection[str]
    ) -> list[CommentRecord]:
        raise CommentsStoreUnavailableError

    async def unanswered_student_replies(
        self, submission_id: uuid.UUID
    ) -> list[CommentRecord]:
        raise CommentsStoreUnavailableError

    async def mark_screened(self, comment_id: uuid.UUID, mark: ScreenMark) -> None:
        raise CommentsStoreUnavailableError


def build_remark_comment_repository(
    session: AsyncSession,
) -> RemarkCommentRepository:
    """The production store of comments over ``session``.

    The one place the store is chosen; tests of other modules substitute this
    factory. Phase 1: the store that refuses every call (module docstring);
    phase 2 builds the store over K1's table on ``session``.
    """
    return _NoCommentsStore()


async def current_task_version(
    session: AsyncSession, authored_document_id: uuid.UUID
) -> str | None:
    """The version of the task in force now, as the attempt ledger records one.

    ``AuthoredDocument.content_hash`` — the ledger's version of a text or
    project task (:func:`~course_supporter.homework.attempt_ledger.task_version_of`).
    A test's version is another key, and a test takes no replies
    (:func:`~course_supporter.homework.path_config.validate_path_config`).
    """
    version: str | None = await session.scalar(
        select(AuthoredDocument.content_hash).where(
            AuthoredDocument.id == authored_document_id
        )
    )
    return version


async def pending_replies(
    session: AsyncSession,
    *,
    submission_id: uuid.UUID,
    student_id: uuid.UUID,
    authored_document_id: uuid.UUID,
    task_type: AssignmentType,
    config: PathConfig | None = None,
    comments: RemarkCommentRepository | None = None,
) -> list[CommentRecord]:
    """The replies the submission ``submission_id`` is to answer (module docstring).

    Asked by the submission's state (``path_selection``), by the reply screen
    in the doors and by the rule of the same work (``attempt_dedup``) — one
    definition for the three. A type that takes no replies costs nothing:
    the switch is read before any query. ``comments`` defaults to the store
    :func:`build_remark_comment_repository` chooses; ``config`` to the cached
    submission-path file.
    """
    if not replies_enabled(task_type, config):
        return []
    _, previous_id, previous_version = await HomeworkRepository(
        session
    ).attempts_before(
        student_id=student_id,
        authored_document_id=authored_document_id,
        excluding=submission_id,
    )
    if previous_id is None:
        return []
    if previous_version != await current_task_version(session, authored_document_id):
        return []
    store = (
        comments if comments is not None else build_remark_comment_repository(session)
    )
    return await store.unanswered_student_replies(previous_id)
