"""Reads and writes of the comments on a review's remarks (task 12a, K3).

Purpose:
    A student replies to a remark of their review; the next review answers the
    reply (product owner's decision, 2026-11-16). Every such comment is a row
    of ``remark_comments`` (K1, :class:`~course_supporter.storage.orm.RemarkComment`).
    The reply core, the reply screen and the replies' state ask for the same
    rows under the same filters — the tenant, the live submission, the live
    student — and a rule spread over several call sites is a rule that drifts,
    so every read and write of a student's reply lives here, beside the values
    it hands in and out.

Interface:
    :class:`CommentRecord` — one comment, as it is read back.
    :class:`NewReply` — a student's reply, as it is written.
    :class:`ScreenMark` — what the reply screen decided about one reply.
    :class:`ThreadScope` — the student's work at one version of a task: the
    rows one thread runs through.
    :class:`MentorAnswer` — the Mentor's answer to one reply, as a review
    hands it in; :class:`AnsweredReply` — a reply and the answer a review
    gave it (task 12a, K4).
    :class:`SqlRemarkCommentRepository` — eight methods: ``lock_replies``,
    ``count_student_replies``, ``add_student_reply``, ``thread``,
    ``unanswered_student_replies``, ``mark_screened``, and K4's
    ``answer_replies`` and ``answers_in_review``. It is the store behind the
    port ``homework.remark_comments.RemarkCommentRepository``, chosen by
    ``homework.remark_comments.build_remark_comment_repository``.

    Writes ``flush`` and never commit: the caller owns the transaction (the
    route commits a reply, the body of the path commits the screen's marks).
    Every read and write is scoped by ``tenant_id``; the reads keep to live
    submissions and live students, as K1 asks of every reader.

    ``hostile_category`` is the safety check's violation
    (``security.schemas.ViolationCategory``) by its value: the storage layer
    holds the string the column holds and does not import the security layer.
    The Mentor's answers (K4) are written by ``answer_replies`` — under each
    reply, in the review that answers it — and read back, with their replies,
    by ``answers_in_review``.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from course_supporter.remark_kinds import CommentAuthor, CommentKind, CommentTopic
from course_supporter.storage.orm import HomeworkSubmission, RemarkComment, Student

_STUDENT = CommentAuthor.STUDENT.value
_MENTOR = CommentAuthor.MENTOR.value


@dataclass(frozen=True, slots=True)
class CommentRecord:
    """One comment on one remark, as the store hands it back.

    Attributes:
        id: The comment.
        submission_id: The submission whose review the comment is attached to:
            for a student's reply, the attempt whose review it answers; for the
            Mentor's answer, the attempt in whose review it is given.
        item_id: The remark: ``c3`` — a criterion, ``c3.p2`` — a point of it.
        parent_id: The comment of the same thread this one answers; ``None``
            for a comment that starts a thread.
        author: Who wrote it.
        kind: Question, objection or comment — a student's; answer — the
            Mentor's.
        body: The text, as it was stored.
        created_at: When it was written; a thread reads in this order.
        answered_in_submission_id: For a student's reply, the submission whose
            review answered it; ``None`` while none has.
        topic: What the reply screen judged a student's reply to be; ``None``
            until it judged it — and for one it could not judge, and always
            for the Mentor's answer.
        topic_confidence: The confidence of the verdict that set ``topic``:
            the topic port's, or the safety check's for a hostile reply.
            ``None`` when that verdict carried none.
        hostile_category: For a hostile reply, the gravest violation the
            safety check named (a ``ViolationCategory`` value); ``None``
            otherwise — and for a hostile one it named none.
    """

    id: uuid.UUID
    submission_id: uuid.UUID
    item_id: str
    parent_id: uuid.UUID | None
    author: CommentAuthor
    kind: CommentKind
    body: str
    created_at: datetime
    answered_in_submission_id: uuid.UUID | None = None
    topic: CommentTopic | None = None
    topic_confidence: float | None = None
    hostile_category: str | None = None


@dataclass(frozen=True, slots=True)
class NewReply:
    """A student's reply as the core hands it to the store: checked, and final.

    Everything but the words is read off the submission the core checked —
    its tenant, its student, its task and the version it was read against —
    never off the request beside it.
    """

    tenant_id: uuid.UUID
    student_id: uuid.UUID
    authored_document_id: uuid.UUID
    task_version: str
    submission_id: uuid.UUID
    item_id: str
    parent_id: uuid.UUID | None
    kind: CommentKind
    body: str


@dataclass(frozen=True, slots=True)
class ScreenMark:
    """What the reply screen decided about one reply.

    ``hostile_category`` is a ``ViolationCategory`` value, set on a hostile
    reply alone.
    """

    topic: CommentTopic
    topic_confidence: float | None
    hostile_category: str | None


@dataclass(frozen=True, slots=True)
class MentorAnswer:
    """The Mentor's answer to one student's reply, as a review hands it in (K4).

    ``reply_id`` is the reply answered; everything else the answer's row
    holds — the item, the student's work at its version — is read off that
    reply, never off the caller.
    """

    reply_id: uuid.UUID
    body: str


@dataclass(frozen=True, slots=True)
class AnsweredReply:
    """A student's reply and the answer one review gave it (K4)."""

    reply: CommentRecord
    answer: CommentRecord


@dataclass(frozen=True, slots=True)
class ThreadScope:
    """The student's work at one version of a task: what one thread runs through.

    A thread of a remark runs through every attempt the student made at the
    same version of the task — a reply on one attempt, its answer in the next
    review, the student's answer to that — and stops at a new version: the
    remark ``c3`` of another version may be another criterion. The same three
    columns K1 keys the student's work by (``student_id``,
    ``authored_document_id``, ``task_version``), within ``tenant_id``.
    """

    tenant_id: uuid.UUID
    student_id: uuid.UUID
    authored_document_id: uuid.UUID
    task_version: str


def _record(row: RemarkComment) -> CommentRecord:
    return CommentRecord(
        id=row.id,
        submission_id=row.submission_id,
        item_id=row.item_id,
        parent_id=row.parent_id,
        author=CommentAuthor(row.author),
        kind=CommentKind(row.kind),
        body=row.body,
        created_at=row.created_at,
        answered_in_submission_id=row.answered_in_submission_id,
        topic=CommentTopic(row.topic) if row.topic is not None else None,
        topic_confidence=row.topic_confidence,
        hostile_category=row.hostile_category,
    )


def _live(stmt: Select[Any]) -> Select[Any]:
    """Keep to comments whose submission and student are both alive (K1)."""
    return (
        stmt.join(
            HomeworkSubmission, HomeworkSubmission.id == RemarkComment.submission_id
        )
        .join(Student, Student.id == RemarkComment.student_id)
        .where(
            HomeworkSubmission.deleted_at.is_(None),
            Student.deleted_at.is_(None),
        )
    )


class SqlRemarkCommentRepository:
    """The store of comments over ``remark_comments`` (K1's table)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_replies(self, submission_id: uuid.UUID) -> None:
        """Hold back every other writer of this submission's replies.

        Until the caller's transaction ends: the submission's row is locked
        ``FOR NO KEY UPDATE``, which every other writer of its replies takes
        first and none of the review's readers does. The limit of replies per
        remark is a count followed by an insert; two requests that counted
        before either inserted would both pass it.
        """
        await self._session.execute(
            select(HomeworkSubmission.id)
            .where(HomeworkSubmission.id == submission_id)
            .with_for_update(key_share=True)
        )

    async def count_student_replies(
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID, item_id: str
    ) -> int:
        """How many replies the student wrote on this remark of this review."""
        count = await self._session.scalar(
            select(func.count())
            .select_from(RemarkComment)
            .where(
                RemarkComment.tenant_id == tenant_id,
                RemarkComment.submission_id == submission_id,
                RemarkComment.item_id == item_id,
                RemarkComment.author == _STUDENT,
            )
        )
        return int(count or 0)

    async def add_student_reply(self, reply: NewReply) -> CommentRecord:
        """Store a student's reply, unanswered and not yet judged."""
        row = RemarkComment(
            tenant_id=reply.tenant_id,
            student_id=reply.student_id,
            authored_document_id=reply.authored_document_id,
            task_version=reply.task_version,
            submission_id=reply.submission_id,
            item_id=reply.item_id,
            parent_id=reply.parent_id,
            author=_STUDENT,
            kind=reply.kind.value,
            body=reply.body,
        )
        self._session.add(row)
        await self._session.flush()
        # The time is the database's (server default): read back what it set.
        await self._session.refresh(row, attribute_names=["created_at"])
        return _record(row)

    async def thread(
        self, scope: ThreadScope, item_ids: Collection[str]
    ) -> list[CommentRecord]:
        """Every comment of the scope's work on these remarks.

        Both authors, oldest first (by ``created_at``, then by ``id``), live
        submissions and a live student only.
        """
        if not item_ids:
            return []
        stmt = _live(select(RemarkComment)).where(
            RemarkComment.tenant_id == scope.tenant_id,
            RemarkComment.student_id == scope.student_id,
            RemarkComment.authored_document_id == scope.authored_document_id,
            RemarkComment.task_version == scope.task_version,
            RemarkComment.item_id.in_(list(item_ids)),
        )
        rows = await self._session.scalars(
            stmt.order_by(RemarkComment.created_at, RemarkComment.id)
        )
        return [_record(row) for row in rows]

    async def unanswered_student_replies(
        self, *, tenant_id: uuid.UUID, submission_ids: Collection[uuid.UUID]
    ) -> list[CommentRecord]:
        """The student's replies on these submissions no review has answered.

        One query for the whole set, each reply once; oldest first (by
        ``created_at``, then by ``id``) across the submissions, judged or not;
        live submissions and a live student only. No submissions, no query.
        """
        if not submission_ids:
            return []
        stmt = _live(select(RemarkComment)).where(
            RemarkComment.tenant_id == tenant_id,
            RemarkComment.submission_id.in_(list(submission_ids)),
            RemarkComment.author == _STUDENT,
            RemarkComment.answered_in_submission_id.is_(None),
        )
        rows = await self._session.scalars(
            stmt.order_by(RemarkComment.created_at, RemarkComment.id)
        )
        return [_record(row) for row in rows]

    async def mark_screened(
        self, *, tenant_id: uuid.UUID, comment_id: uuid.UUID, mark: ScreenMark
    ) -> None:
        """Record what the reply screen decided about one student's reply.

        A Mentor's comment is never marked: K1's table refuses it a topic, and
        the filter on the author keeps the update off it.
        """
        await self._session.execute(
            update(RemarkComment)
            .where(
                RemarkComment.id == comment_id,
                RemarkComment.tenant_id == tenant_id,
                RemarkComment.author == _STUDENT,
            )
            .values(
                topic=mark.topic.value,
                topic_confidence=mark.topic_confidence,
                hostile_category=mark.hostile_category,
            )
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()

    async def answer_replies(
        self,
        *,
        tenant_id: uuid.UUID,
        submission_id: uuid.UUID,
        answers: Sequence[MentorAnswer],
    ) -> None:
        """Keep the Mentor's answers given in the review of ``submission_id`` (K4).

        Each answer is a comment of the Mentor (``kind = answer``) under its
        reply (``parent_id``), on the reply's item and in the reply's branch —
        its student, task and version — attached to ``submission_id``, the
        review that gives it; the reply is marked answered by that submission
        (``answered_in_submission_id``).

        Once per reply and review: an answer ``submission_id`` already gave a
        reply is rewritten in place, so the stage run again in one
        transaction leaves one answer, never two — and nothing is deleted, so
        no comment written under an answer goes with it. Flushes, never
        commits: the answers and the marks are committed with what the caller
        commits next, or rolled back with it, all together.

        Raises:
            ValueError: an answer names no student's reply of this tenant, or
                a reply another review has answered.
        """
        if not answers:
            return
        ids = [answer.reply_id for answer in answers]
        replies = {
            row.id: row
            for row in await self._session.scalars(
                select(RemarkComment).where(
                    RemarkComment.tenant_id == tenant_id,
                    RemarkComment.id.in_(ids),
                    RemarkComment.author == _STUDENT,
                )
            )
        }
        unknown = sorted(str(i) for i in ids if i not in replies)
        if unknown:
            msg = f"no student's reply of this tenant: {unknown}"
            raise ValueError(msg)
        elsewhere = sorted(
            str(row.id)
            for row in replies.values()
            if row.answered_in_submission_id not in (None, submission_id)
        )
        if elsewhere:
            msg = f"replies another review has answered: {elsewhere}"
            raise ValueError(msg)
        given = {
            row.parent_id: row
            for row in await self._session.scalars(
                select(RemarkComment).where(
                    RemarkComment.tenant_id == tenant_id,
                    RemarkComment.submission_id == submission_id,
                    RemarkComment.author == _MENTOR,
                    RemarkComment.parent_id.in_(ids),
                )
            )
        }
        for answer in answers:
            reply = replies[answer.reply_id]
            earlier = given.get(answer.reply_id)
            if earlier is not None:
                earlier.body = answer.body
            else:
                self._session.add(
                    RemarkComment(
                        tenant_id=tenant_id,
                        student_id=reply.student_id,
                        authored_document_id=reply.authored_document_id,
                        task_version=reply.task_version,
                        submission_id=submission_id,
                        item_id=reply.item_id,
                        parent_id=reply.id,
                        author=_MENTOR,
                        kind=CommentKind.ANSWER.value,
                        body=answer.body,
                    )
                )
            reply.answered_in_submission_id = submission_id
        await self._session.flush()

    async def answers_in_review(
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID
    ) -> list[AnsweredReply]:
        """The answers the review of ``submission_id`` gave, each with its reply.

        In the order of the replies (by ``created_at``, then by ``id``); a live
        submission and a live student only, as every read.
        """
        reply = aliased(RemarkComment)
        stmt = (
            _live(select(RemarkComment, reply))
            .join(reply, reply.id == RemarkComment.parent_id)
            .where(
                RemarkComment.tenant_id == tenant_id,
                RemarkComment.submission_id == submission_id,
                RemarkComment.author == _MENTOR,
                reply.tenant_id == tenant_id,
                reply.author == _STUDENT,
            )
            .order_by(reply.created_at, reply.id)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            AnsweredReply(reply=_record(asked), answer=_record(given))
            for given, asked in rows
        ]
