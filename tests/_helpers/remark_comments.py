"""A store of remark comments in memory, for the tests of task 12a (K3).

Phase 1 of K3 has no comments table (it is K1's), so every reader of the
comments is tested against this double of the port
(:class:`~course_supporter.homework.remark_comments.RemarkCommentRepository`).
It keeps to the port's contract as its docstrings state it — the scope of a
thread, the order, what counts as a student's unanswered reply — so a test
green here asks of the real store exactly what phase 2 must give.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from course_supporter.homework.remark_comments import (
    CommentRecord,
    NewReply,
    ScreenMark,
    ThreadScope,
)
from course_supporter.reply_kinds import CommentAuthor

_EPOCH = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


class FakeRemarkComments:
    """The port in memory; what was asked of it is kept for the assertions.

    ``scopes`` names the thread scope of each submission a comment may be
    attached to — what the real store reads off ``homework_submissions``.
    """

    def __init__(
        self,
        comments: Iterable[CommentRecord] = (),
        *,
        scopes: dict[uuid.UUID, ThreadScope] | None = None,
    ) -> None:
        self.comments: list[CommentRecord] = list(comments)
        self.scopes: dict[uuid.UUID, ThreadScope] = dict(scopes or {})
        self.locked: list[uuid.UUID] = []
        self.marks: dict[uuid.UUID, ScreenMark] = {}
        self.added: list[NewReply] = []

    async def lock_replies(self, submission_id: uuid.UUID) -> None:
        self.locked.append(submission_id)

    async def count_student_replies(
        self, *, submission_id: uuid.UUID, item_id: str
    ) -> int:
        return sum(
            1
            for c in self.comments
            if c.submission_id == submission_id
            and c.item_id == item_id
            and c.author is CommentAuthor.STUDENT
        )

    async def add_student_reply(self, reply: NewReply) -> CommentRecord:
        self.added.append(reply)
        record = CommentRecord(
            id=uuid.uuid4(),
            submission_id=reply.submission_id,
            item_id=reply.item_id,
            parent_id=reply.parent_id,
            author=CommentAuthor.STUDENT,
            kind=reply.kind,
            body=reply.body,
            created_at=_EPOCH + timedelta(minutes=len(self.comments)),
        )
        self.comments.append(record)
        return record

    async def thread(
        self, scope: ThreadScope, item_ids: Collection[str]
    ) -> list[CommentRecord]:
        return sorted(
            (
                c
                for c in self.comments
                if self.scopes.get(c.submission_id) == scope and c.item_id in item_ids
            ),
            key=lambda c: (c.created_at, c.id),
        )

    async def unanswered_student_replies(
        self, submission_id: uuid.UUID
    ) -> list[CommentRecord]:
        return sorted(
            (
                c
                for c in self.comments
                if c.submission_id == submission_id
                and c.author is CommentAuthor.STUDENT
                and c.answered_in_submission_id is None
            ),
            key=lambda c: (c.created_at, c.id),
        )

    async def mark_screened(self, comment_id: uuid.UUID, mark: ScreenMark) -> None:
        self.marks[comment_id] = mark
        self.comments = [
            replace(
                c,
                topic=mark.topic,
                topic_confidence=mark.topic_confidence,
                hostile_category=mark.hostile_category,
            )
            if c.id == comment_id
            else c
            for c in self.comments
        ]


def a_reply(
    submission_id: uuid.UUID,
    *,
    item_id: str = "c1",
    body: str = "Чому цей критерій не виконано? Функція є в рядку 12.",
    kind: str = "question",
    minute: int = 0,
    parent_id: uuid.UUID | None = None,
    author: CommentAuthor = CommentAuthor.STUDENT,
    answered_in: uuid.UUID | None = None,
) -> CommentRecord:
    """One stored comment, a student's unanswered question by default."""
    return CommentRecord(
        id=uuid.uuid4(),
        submission_id=submission_id,
        item_id=item_id,
        parent_id=parent_id,
        author=author,
        kind=kind,
        body=body,
        created_at=_EPOCH + timedelta(minutes=minute),
        answered_in_submission_id=answered_in,
    )
