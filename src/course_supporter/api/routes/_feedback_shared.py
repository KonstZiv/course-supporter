"""One projection of what a student said, for the two doors that serve it.

Both entry points of a touch — the portal session and the caller's channel —
answer with the same shape, and the submission detail serves that shape again
on the next read. Three call sites, one function: a projection copied into two
route modules is a projection that will one day differ between them, which is
the same reason the decision itself lives in a single core.

The replies to remarks (task 12a, K3) go through the same two doors, so their
projections live here too — and the one way both doors reach the store of
comments, :func:`remark_comments`, a dependency a test overrides.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.schemas import (
    FeedbackTouch,
    RemarkComment,
    RemarkThread,
    RemarkThreads,
)
from course_supporter.feedback_kinds import FeedbackKind, FeedbackValue
from course_supporter.homework.remark_comments import (
    CommentRecord,
    RemarkCommentRepository,
    build_remark_comment_repository,
)
from course_supporter.homework.reply_core import ReplyThreads
from course_supporter.storage.orm import StudentFeedback


def to_touch(row: StudentFeedback) -> FeedbackTouch:
    """The stored row as the answer served back to a caller."""
    return FeedbackTouch(
        kind=FeedbackKind(row.kind),
        value=FeedbackValue(row.value),
        updated_at=row.updated_at,
    )


def remark_comments(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RemarkCommentRepository:
    """The store of comments over the request's session (task 12a, K3).

    Building it touches nothing: the core asks the type's switch before it
    asks the store anything, so a type that takes no replies never reaches it.
    """
    return build_remark_comment_repository(session)


def to_remark_comment(record: CommentRecord) -> RemarkComment:
    """A comment as both doors serve it — without the screen's marks."""
    return RemarkComment(
        id=record.id,
        submission_id=record.submission_id,
        item_id=record.item_id,
        parent_id=record.parent_id,
        author=record.author,
        kind=record.kind,
        body=record.body,
        created_at=record.created_at,
        answered_in_submission_id=record.answered_in_submission_id,
    )


def to_remark_threads(read: ReplyThreads) -> RemarkThreads:
    """A review's threads, and whether it takes a reply, as both doors serve them."""
    return RemarkThreads(
        accepting_replies=read.accepting_replies,
        threads=[
            RemarkThread(
                item_id=thread.item_id,
                comments=[to_remark_comment(c) for c in thread.comments],
            )
            for thread in read.threads
        ],
    )
