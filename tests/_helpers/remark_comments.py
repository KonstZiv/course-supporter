"""A store of remark comments in memory, for the unit tests of task 12a (K3).

The unit tests of the reply core, the doors and the reply screen run without a
database, against this double of the port
(:class:`~course_supporter.homework.remark_comments.RemarkCommentRepository`).
It keeps to the port's contract as its docstrings state it — the scope of a
thread, the order, what counts as a student's unanswered reply — and records
the tenant every call was scoped by. The store over K1's table, with the
filters a double cannot show (tenant, live submission, live student), is
pinned on a real database (``tests/integration/test_remark_comment_repository_db.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from course_supporter.homework.remark_comments import (
    AnsweredReply,
    CommentRecord,
    MentorAnswer,
    NewReply,
    ScreenMark,
    ThreadScope,
)
from course_supporter.remark_kinds import CommentAuthor, CommentKind
from course_supporter.storage.orm import RemarkComment

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import HomeworkSubmission

_EPOCH = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


class FakeRemarkComments:
    """The port in memory; what was asked of it is kept for the assertions.

    ``scopes`` names the thread scope of each submission a comment may be
    attached to — what the real store reads off the rows themselves.
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
        self.tenants: list[uuid.UUID] = []
        self.asked_for: list[tuple[uuid.UUID, ...]] = []

    async def lock_replies(self, submission_id: uuid.UUID) -> None:
        self.locked.append(submission_id)

    async def count_student_replies(
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID, item_id: str
    ) -> int:
        self.tenants.append(tenant_id)
        return sum(
            1
            for c in self.comments
            if c.submission_id == submission_id
            and c.item_id == item_id
            and c.author is CommentAuthor.STUDENT
        )

    async def add_student_reply(self, reply: NewReply) -> CommentRecord:
        self.added.append(reply)
        self.tenants.append(reply.tenant_id)
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
        self.tenants.append(scope.tenant_id)
        return sorted(
            (
                c
                for c in self.comments
                if self.scopes.get(c.submission_id) == scope and c.item_id in item_ids
            ),
            key=lambda c: (c.created_at, c.id),
        )

    async def unanswered_student_replies(
        self, *, tenant_id: uuid.UUID, submission_ids: Collection[uuid.UUID]
    ) -> list[CommentRecord]:
        self.tenants.append(tenant_id)
        self.asked_for.append(tuple(submission_ids))
        return sorted(
            (
                c
                for c in self.comments
                if c.submission_id in submission_ids
                and c.author is CommentAuthor.STUDENT
                and c.answered_in_submission_id is None
            ),
            key=lambda c: (c.created_at, c.id),
        )

    async def mark_screened(
        self, *, tenant_id: uuid.UUID, comment_id: uuid.UUID, mark: ScreenMark
    ) -> None:
        self.tenants.append(tenant_id)
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

    async def answer_replies(
        self,
        *,
        tenant_id: uuid.UUID,
        submission_id: uuid.UUID,
        answers: Sequence[MentorAnswer],
    ) -> None:
        """K4's write, in memory: once per reply and review, the reply marked."""
        self.tenants.append(tenant_id)
        for answer in answers:
            reply = next(c for c in self.comments if c.id == answer.reply_id)
            earlier = next(
                (
                    c
                    for c in self.comments
                    if c.parent_id == reply.id
                    and c.author is CommentAuthor.MENTOR
                    and c.submission_id == submission_id
                ),
                None,
            )
            if earlier is not None:
                self.comments[self.comments.index(earlier)] = replace(
                    earlier, body=answer.body
                )
            else:
                self.comments.append(
                    CommentRecord(
                        id=uuid.uuid4(),
                        submission_id=submission_id,
                        item_id=reply.item_id,
                        parent_id=reply.id,
                        author=CommentAuthor.MENTOR,
                        kind=CommentKind.ANSWER,
                        body=answer.body,
                        created_at=_EPOCH + timedelta(minutes=len(self.comments)),
                    )
                )
            self.comments[self.comments.index(reply)] = replace(
                reply, answered_in_submission_id=submission_id
            )

    async def answers_in_review(
        self, *, tenant_id: uuid.UUID, submission_id: uuid.UUID
    ) -> list[AnsweredReply]:
        self.tenants.append(tenant_id)
        by_id = {c.id: c for c in self.comments}
        pairs = [
            AnsweredReply(reply=by_id[c.parent_id], answer=c)
            for c in self.comments
            if c.author is CommentAuthor.MENTOR
            and c.submission_id == submission_id
            and c.parent_id in by_id
        ]
        return sorted(pairs, key=lambda p: (p.reply.created_at, p.reply.id))


def a_reply(
    submission_id: uuid.UUID,
    *,
    item_id: str = "c1",
    body: str = "Чому цей критерій не виконано? Функція є в рядку 12.",
    kind: CommentKind = CommentKind.QUESTION,
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


async def a_stored_comment(
    session: AsyncSession,
    submission: HomeworkSubmission,
    *,
    item_id: str = "c1",
    body: str = "Чому цей критерій не виконано? Функція є в рядку 12.",
    author: CommentAuthor = CommentAuthor.STUDENT,
    kind: CommentKind = CommentKind.QUESTION,
    parent_id: uuid.UUID | None = None,
    answered_in: uuid.UUID | None = None,
    minute: int | None = 0,
    task_version: str | None = None,
) -> RemarkComment:
    """One row of K1's table on ``submission``'s review, as K3 or K4 writes it.

    The student's work is read off the submission — its tenant, student, task
    and version — as the store reads it; ``task_version`` overrides the last
    (K1's table needs one). ``created_at`` is set from ``minute``, so a
    thread's order is the test's, not the clock's; ``minute=None`` leaves it
    to the database, as a reply written through the store is (the
    transaction's time — the order is then the ids').
    """
    version = task_version if task_version is not None else submission.task_version
    assert version is not None, "K1's table keys every comment by a version"
    row = RemarkComment(
        tenant_id=submission.tenant_id,
        student_id=submission.student_id,
        authored_document_id=submission.authored_document_id,
        task_version=version,
        submission_id=submission.id,
        item_id=item_id,
        parent_id=parent_id,
        author=author.value,
        kind=kind.value,
        body=body,
        answered_in_submission_id=answered_in,
    )
    if minute is not None:
        row.created_at = _EPOCH + timedelta(minutes=minute)
    session.add(row)
    await session.flush()
    return row
