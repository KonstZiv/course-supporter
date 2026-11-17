"""The comments on a review's remarks, as the rest of the code sees them (task 12a, K3).

Purpose:
    A student replies to a remark of their review; the next review answers the
    reply (product owner's decision, 2026-11-16). Every such comment is a row
    of K1's table ``remark_comments``. This module is what the reply core, the
    reply screen, the submission's state and the rule of the same work depend
    on instead of that table: the port that stores and reads the comments,
    the one place its store is chosen, and the ONE definition of "the replies
    the next review has to answer" — so the four cannot come to mean four
    different sets of replies.

Interface:
    :class:`CommentRecord`, :class:`NewReply`, :class:`ScreenMark`,
    :class:`ThreadScope`, :class:`MentorAnswer`, :class:`AnsweredReply` — the
    values the port speaks in, defined beside the store
    (``storage/remark_comment_repository.py``) and named here for the port's
    readers.
    :class:`RemarkCommentRepository` — the port (a structural ``Protocol``):
    eight methods — the last two, the Mentor's answers, K4's — the contract
    of each in its docstring; every read and write is scoped by the tenant.
    :func:`build_remark_comment_repository` — the one place the store is
    chosen: the store over K1's table. Tests of other modules substitute it.
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
    other ones under the same ids. An attempt with no recorded version (one
    reviewed before the attempt ledger) has no replies: K1's table keys every
    comment by a version. A type that takes no replies
    (:func:`~course_supporter.homework.path_config.replies_enabled`) has none,
    and nothing is asked of the store.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select

from course_supporter.homework.path_config import replies_enabled
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import AuthoredDocument
from course_supporter.storage.remark_comment_repository import (
    AnsweredReply,
    CommentRecord,
    MentorAnswer,
    NewReply,
    ScreenMark,
    SqlRemarkCommentRepository,
    ThreadScope,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.path_config import PathConfig
    from course_supporter.models.source import AssignmentType

__all__ = [
    "AnsweredReply",
    "CommentRecord",
    "MentorAnswer",
    "NewReply",
    "RemarkCommentRepository",
    "ScreenMark",
    "ThreadScope",
    "build_remark_comment_repository",
    "current_task_version",
    "pending_replies",
]


class RemarkCommentRepository(Protocol):
    """The store of comments on remarks — the port of this module.

    Writes ``flush`` and never commit: the caller owns the transaction (the
    route commits the reply, the body of the path commits the screen's marks
    with what it writes next). Reads keep to live submissions and live
    students.
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
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID, item_id: str
    ) -> int:
        """How many replies the student wrote on this remark of this review."""
        ...

    async def add_student_reply(self, reply: NewReply) -> CommentRecord:
        """Store a student's reply, unanswered and not yet judged."""
        ...

    async def thread(
        self, scope: ThreadScope, item_ids: Collection[str]
    ) -> list[CommentRecord]:
        """Every comment of the scope's work on these remarks.

        Both authors, oldest first (by ``created_at``, then by ``id``).
        """
        ...

    async def unanswered_student_replies(
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID
    ) -> list[CommentRecord]:
        """The student's replies on this submission no review has answered.

        Oldest first, judged or not.
        """
        ...

    async def mark_screened(
        self, *, tenant_id: uuid.UUID, comment_id: uuid.UUID, mark: ScreenMark
    ) -> None:
        """Record what the reply screen decided about one student's reply."""
        ...

    async def answer_replies(
        self,
        *,
        tenant_id: uuid.UUID,
        submission_id: uuid.UUID,
        answers: Sequence[MentorAnswer],
    ) -> None:
        """Keep the Mentor's answers given in the review of ``submission_id``.

        Each under its reply, which is marked answered by that submission;
        once per reply and review — an answer given again is rewritten in
        place (task 12a, K4).
        """
        ...

    async def answers_in_review(
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID
    ) -> list[AnsweredReply]:
        """The answers the review of ``submission_id`` gave, each with its reply.

        In the order of the replies.
        """
        ...


def build_remark_comment_repository(
    session: AsyncSession,
) -> RemarkCommentRepository:
    """The production store of comments over ``session``: K1's table.

    The one place the store is chosen; tests of other modules substitute this
    factory, and the reply screen reaches it through this module so that one
    substitution serves every reader.
    """
    return SqlRemarkCommentRepository(session)


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
    tenant_id: uuid.UUID,
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
    if previous_id is None or previous_version is None:
        return []
    if previous_version != await current_task_version(session, authored_document_id):
        return []
    store = (
        comments if comments is not None else build_remark_comment_repository(session)
    )
    return await store.unanswered_student_replies(
        tenant_id=tenant_id, submission_id=previous_id
    )
