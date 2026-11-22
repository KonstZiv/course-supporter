"""The one place a student's reply to a remark is decided and read (task 12a, K3).

A student answers a remark of their review — a question, an objection, a
comment — and the next review answers each reply (product owner's decision,
2026-11-16). Two doors lead here, as they lead to the touch
(:mod:`~course_supporter.homework.feedback_core`): the student's own portal
session and the caller's channel with a tenant key. They differ in exactly
one thing — how they find the submission and which student they name — and
everything after that is here, so the two cannot answer the same situation
differently.

The order of the refusals is load-bearing, not stylistic:

1. no submission, one that is not this student's, a soft-deleted one — and a
   type that takes no replies
   (:func:`~course_supporter.homework.path_config.replies_enabled`) — the
   one generic "not found" of the read path
   (:data:`~course_supporter.homework.feedback_core.SUBMISSION_NOT_FOUND`):
   a reply must not become the surface that tells which submissions exist,
   nor where replies would be taken if they were on;
2. where the reply goes — the review replies go to (``not_latest_review``),
   a remark of that review (``unknown_item``), a parent in the remark's
   thread (``parent_not_in_thread``), room left on the remark
   (``comment_limit_reached``);
3. the words — none (``comment_empty``), too many (``comment_too_long``),
   refused by the text screen (``comment_rejected``).

Where before the words: a reply that cannot be taken where it was sent is not
worth correcting, and asking a student to shorten a text that would then be
refused for its place is a step that leads nowhere.

The review replies go to:
    A reply is answered by the student's NEXT submission, which answers the
    replies to its previous attempt
    (:func:`~course_supporter.homework.remark_comments.pending_replies`). So
    a reply is taken only where that submission will look:

    * the review is written (``completed`` or ``delivered``, with a text —
      :func:`~course_supporter.homework.feedback_core.review_is_readable`);
    * no later submission of the student on the task is reviewed or on its
      way to a review — a later one that ended without a review (refused at
      the door or by the safety check, judged off-task, failed) leaves this
      review the one the next submission answers, as the attempt ledger
      leaves it the previous attempt;
    * the review was read against the version of the task in force now: the
      next submission answers no reply to a review of another version — and a
      review with no recorded version (one written before the attempt
      ledger) is no such review: K1's table keys every comment by a version.

    Each of the three answers ``not_latest_review``: in each the reply would
    be stored and never answered.

The remarks of a review:
    Its verdict rows, by ``item_id`` (``c3`` — a criterion, ``c3.p2`` — a
    point). A review repeated by the rule of the same work (task 13, K3) has
    no rows of its own: it IS the review of the attempt it repeats, so its
    remarks are that attempt's rows (:func:`review_items`). Without that, a
    student whose same work got a copy could reply to nothing until the work
    changed.

The limit:
    At most :data:`REPLIES_PER_REMARK` replies of the student on one remark of
    one review — between two submissions. The store holds back other writers
    of the submission's replies before the count
    (:meth:`~course_supporter.homework.remark_comments.RemarkCommentRepository.lock_replies`),
    so two replies sent at once cannot both take the last place.

The words:
    The checks a student's comment to a submission meets
    (:mod:`~course_supporter.homework.student_text`): line breaks as ``\\n``,
    edges trimmed, up to :data:`REPLY_MAX_CHARS` code points, the one text
    screen in ``signal`` mode — refused for direction overrides, tag and
    control characters and text that is not Unicode; a phrase addressed to a
    model is only noticed, and the reply screen shows it to the safety check.
    What is stored is the screened text in NFC.

Interface:
    :func:`post_reply` — record a reply; :func:`read_threads` — the threads of
    a review's remarks and whether a reply to it would be taken
    (``accepting_replies``, the write's own rule — task 12a, K3b);
    :func:`review_facts` and :func:`review_items` — what both read about the
    review; :class:`ReplyRefusal` — the codes of the 422
    refusals, each with ``details`` as an object (``details.max`` for the two
    limits, ``details.category`` for the text screen's refusal).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import structlog
from fastapi import HTTPException
from sqlalchemy import and_, exists, or_, select

from course_supporter.homework.attempt_dedup import DEDUP_RECEIPT_KEY
from course_supporter.homework.feedback_core import (
    SUBMISSION_NOT_FOUND,
    review_is_readable,
)
from course_supporter.homework.path_config import replies_enabled
from course_supporter.homework.remark_comments import (
    CommentRecord,
    NewReply,
    RemarkCommentRepository,
    ThreadScope,
)
from course_supporter.homework.student_text import (
    normalized_student_text,
    screened_student_text,
)
from course_supporter.models.source import AssignmentType
from course_supporter.remark_kinds import CommentKind
from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.storage.homework_repository import ATTEMPT_STATUSES
from course_supporter.storage.orm import (
    AuthoredDocument,
    HomeworkStatus,
    HomeworkSubmission,
    SubmissionCriterionVerdict,
)
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.path_config import PathConfig
    from course_supporter.models.review_structure import ReplyKind

__all__ = [
    "REPLIES_PER_REMARK",
    "REPLY_MAX_CHARS",
    "REPLY_SOURCE",
    "ReplyRefusal",
    "ReplyThreads",
    "ReviewFacts",
    "Thread",
    "post_reply",
    "read_threads",
    "review_facts",
    "review_items",
]

logger = structlog.get_logger(__name__)

REPLY_MAX_CHARS: Final = 2_000
"""The most a reply may hold, in code points — ``details.max`` of the refusal.

The student's own limit, the code's to keep; the table admits up to
:data:`~course_supporter.remark_kinds.COMMENT_MAX_CHARS` for any comment, the
Mentor's answer included (K1).
"""


REPLIES_PER_REMARK: Final = 5
"""The most replies a student writes on one remark between two submissions."""

REPLY_SOURCE: Final = "student_reply"
"""What a reply is named by the text screen: in a refusal's detail and in a
flag the reply screen shows the safety check."""


class ReplyRefusal(StrEnum):
    """The code of a 422 refusal of a reply; ``details`` is an object.

    The six of the contract (K3 brief, 2026-11-16) and one it did not name:
    ``comment_rejected``, the text screen's refusal — the checks a student's
    comment to a submission meets (``STUDENT_NOTE_REJECTED`` there) refuse a
    reply too, and a refusal needs a code of its own.
    """

    COMMENT_EMPTY = "comment_empty"
    COMMENT_TOO_LONG = "comment_too_long"
    COMMENT_REJECTED = "comment_rejected"
    COMMENT_LIMIT_REACHED = "comment_limit_reached"
    NOT_LATEST_REVIEW = "not_latest_review"
    UNKNOWN_ITEM = "unknown_item"
    PARENT_NOT_IN_THREAD = "parent_not_in_thread"


_ENDED_WITHOUT_REVIEW: Final = frozenset(
    {
        HomeworkStatus.REJECTED.value,
        HomeworkStatus.MISMATCH.value,
        HomeworkStatus.FAILED.value,
    }
)
"""The later submissions that leave a review the one replies go to.

Refused at the door or by the safety check, judged off-task, failed: none of
them is an attempt (``homework_repository.ATTEMPT_STATUSES``), so the next
submission still reads this review as its previous attempt. Every other
status is a review written or on its way, and replies to this one would wait
for an answer nothing is going to give.
"""


@dataclass(frozen=True, slots=True)
class ReviewFacts:
    """What the database says about the review a reply would be about.

    Attributes:
        task_type: The task's type; ``None`` when the anchor is not a task.
        task_version: The version of the task in force now
            (:func:`~course_supporter.homework.remark_comments.current_task_version`).
        later_statuses: The statuses of the student's live submissions on the
            task made after this one.
        items: The remarks of the review, in the order of its verdict rows
            (:func:`review_items`).
    """

    task_type: AssignmentType | None
    task_version: str | None
    later_statuses: tuple[str, ...]
    items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Thread:
    """The comments on one remark, oldest first."""

    item_id: str
    comments: tuple[CommentRecord, ...]


@dataclass(frozen=True, slots=True)
class ReplyThreads:
    """What the read of a review's threads answers (task 12a, K3b).

    Attributes:
        accepting_replies: Whether a reply to this review would be taken —
            :func:`_accepts_replies`, the write's own rule up to
            ``not_latest_review``. ``False`` for a type that takes no replies,
            for a review that is not the one replies go to, and for one with
            no recorded version.
        threads: The threads on its remarks, in the order of the remarks.
    """

    accepting_replies: bool
    threads: tuple[Thread, ...]


def _not_found() -> HTTPException:
    """The one refusal every access failure gets — the read path's own words."""
    return HTTPException(status_code=404, detail=SUBMISSION_NOT_FOUND)


def _refusal(code: ReplyRefusal, **details: Any) -> HTTPException:
    """A 422 with ``{code, details}``; ``details`` is an object, empty or not."""
    return HTTPException(
        status_code=422, detail={"code": code.value, "details": details}
    )


def _owned(
    submission: HomeworkSubmission | None, student_id: uuid.UUID
) -> HomeworkSubmission:
    """The submission, when it is alive and this student's; else the 404.

    The doors already filter on both — the check is made again here for the
    reason ``feedback_core`` gives: a value a core is told can disagree with
    the object it describes, and a door rewritten one day would then write
    one student's words under another's name.
    """
    if (
        submission is None
        or submission.deleted_at is not None
        or submission.student_id != student_id
    ):
        raise _not_found()
    return submission


def _takes_replies(facts: ReviewFacts, config: PathConfig | None) -> bool:
    return facts.task_type is not None and replies_enabled(facts.task_type, config)


def _scope_of(submission: HomeworkSubmission) -> ThreadScope | None:
    """The student's work the threads of this review run through.

    Read off the submission — its tenant, its student, its task, the version
    it was read against — never off the request beside it. ``None`` for a
    submission with no recorded version (reviewed before the attempt
    ledger): K1's table keys every comment by a version, so it has no thread.
    """
    if submission.task_version is None:
        return None
    return ThreadScope(
        tenant_id=submission.tenant_id,
        student_id=submission.student_id,
        authored_document_id=submission.authored_document_id,
        task_version=submission.task_version,
    )


def _replies_go_here(submission: HomeworkSubmission, facts: ReviewFacts) -> bool:
    """Is this the review the next submission answers? (module docstring)

    A review with no recorded version of the task is none: the next
    submission reads no reply to it, and K1's table could not hold one.
    """
    return (
        review_is_readable(submission)
        and all(status in _ENDED_WITHOUT_REVIEW for status in facts.later_statuses)
        and submission.task_version is not None
        and submission.task_version == facts.task_version
    )


def _open_scope(
    submission: HomeworkSubmission, facts: ReviewFacts
) -> ThreadScope | None:
    """Where a reply to this review would be written — ``None``: not here.

    The one rule of "the review replies go to" (module docstring), asked by
    the write and by the read alike: the student's work at a recorded version
    (:func:`_scope_of`), on the review the next submission answers
    (:func:`_replies_go_here`). ``None`` is ``not_latest_review`` to a reply
    and ``accepting_replies: false`` to a read.
    """
    scope = _scope_of(submission)
    if scope is None or not _replies_go_here(submission, facts):
        return None
    return scope


def _accepts_replies(
    submission: HomeworkSubmission, facts: ReviewFacts, config: PathConfig | None
) -> bool:
    """Would a reply here pass every check up to ``not_latest_review``?

    The type takes replies (else the reply's 404) and the review is the one
    replies go to (else ``not_latest_review``) — the same two questions
    :func:`post_reply` asks, in its order; the limit per remark is a remark's
    question, not the review's, and is not asked. Read from the facts already
    read: no query of its own.
    """
    return _takes_replies(facts, config) and _open_scope(submission, facts) is not None


def _checked_text(body: str) -> str:
    """The reply's words as they are stored, or the refusal of the third kind."""
    text = normalized_student_text(body)
    if not text:
        raise _refusal(ReplyRefusal.COMMENT_EMPTY)
    if len(text) > REPLY_MAX_CHARS:
        raise _refusal(ReplyRefusal.COMMENT_TOO_LONG, max=REPLY_MAX_CHARS)
    try:
        return screened_student_text(text, name=REPLY_SOURCE)
    except SecurityRejectedError as exc:
        raise _refusal(
            ReplyRefusal.COMMENT_REJECTED, category=exc.category.value
        ) from exc


async def post_reply(
    session: AsyncSession,
    *,
    submission: HomeworkSubmission | None,
    student_id: uuid.UUID,
    item_id: str,
    kind: ReplyKind,
    body: str,
    parent_id: uuid.UUID | None,
    comments: RemarkCommentRepository,
    config: PathConfig | None = None,
) -> CommentRecord:
    """Record this student's reply to a remark of this submission's review.

    Args:
        session: Database session; the caller commits.
        submission: The submission as the door resolved it under its own
            access filter — ``None`` when anything about that failed.
        student_id: The student the door authenticated; checked against the
            submission's owner rather than trusted.
        item_id: The remark (``c3``, ``c3.p2``).
        kind: Question, objection or comment.
        body: The words, as they arrived.
        parent_id: The comment of the remark's thread this one answers;
            ``None`` for a reply to the remark itself.
        comments: The store of comments.
        config: The submission-path file; the cached one by default.

    Returns:
        The stored reply.

    Raises:
        HTTPException: 404 with the read path's "not found" (module docstring,
            1); 422 with ``{code, details}`` and a :class:`ReplyRefusal` code
            (2 and 3).
    """
    live = _owned(submission, student_id)
    facts = await review_facts(session, live)
    if not _takes_replies(facts, config):
        raise _not_found()
    scope = _open_scope(live, facts)
    if scope is None:
        raise _refusal(ReplyRefusal.NOT_LATEST_REVIEW)
    if item_id not in facts.items:
        raise _refusal(ReplyRefusal.UNKNOWN_ITEM)
    if parent_id is not None:
        thread = await comments.thread(scope, (item_id,))
        if all(comment.id != parent_id for comment in thread):
            raise _refusal(ReplyRefusal.PARENT_NOT_IN_THREAD)
    await comments.lock_replies(live.id)
    written = await comments.count_student_replies(
        tenant_id=live.tenant_id, submission_id=live.id, item_id=item_id
    )
    if written >= REPLIES_PER_REMARK:
        raise _refusal(ReplyRefusal.COMMENT_LIMIT_REACHED, max=REPLIES_PER_REMARK)
    text = _checked_text(body)

    reply = await comments.add_student_reply(
        NewReply(
            tenant_id=scope.tenant_id,
            student_id=scope.student_id,
            authored_document_id=scope.authored_document_id,
            task_version=scope.task_version,
            submission_id=live.id,
            item_id=item_id,
            parent_id=parent_id,
            kind=CommentKind(kind),
            body=text,
        )
    )
    # Never the words: the log is read by more people than the review is.
    logger.info(
        "remark_reply_recorded",
        submission_id=str(live.id),
        comment_id=str(reply.id),
        item_id=item_id,
        kind=kind,
        in_thread=parent_id is not None,
    )
    return reply


async def read_threads(
    session: AsyncSession,
    *,
    submission: HomeworkSubmission | None,
    student_id: uuid.UUID,
    comments: RemarkCommentRepository,
    config: PathConfig | None = None,
) -> ReplyThreads:
    """The threads on the remarks of this submission's review, and the sign.

    Each thread runs through every attempt of the student at the same version
    of the task (:class:`~course_supporter.homework.remark_comments.ThreadScope`),
    oldest comment first; nesting is the comments' ``parent_id``. Threads come
    in the order of the review's remarks, and a remark nobody commented on
    has none. A type that takes no replies, and a review with no remark, have
    no threads.

    ``accepting_replies`` tells the portal whether to offer a reply here —
    without it a type that takes no replies and a review nobody has commented
    on read the same. It is the write's own rule (:func:`_accepts_replies`),
    read from the facts this read reads anyway: a type that takes no replies
    costs no query more than before.

    Raises:
        HTTPException: 404 with the read path's "not found" for a submission
            the student cannot read.
    """
    live = _owned(submission, student_id)
    facts = await review_facts(session, live)
    accepting = _accepts_replies(live, facts, config)
    scope = _scope_of(live)
    if not _takes_replies(facts, config) or not facts.items or scope is None:
        return ReplyThreads(accepting_replies=accepting, threads=())
    found = await comments.thread(scope, facts.items)
    by_item: dict[str, list[CommentRecord]] = {}
    for comment in sorted(found, key=lambda c: (c.created_at, c.id)):
        by_item.setdefault(comment.item_id, []).append(comment)
    return ReplyThreads(
        accepting_replies=accepting,
        threads=tuple(
            Thread(item_id=item, comments=tuple(by_item[item]))
            for item in facts.items
            if item in by_item
        ),
    )


async def review_facts(
    session: AsyncSession, submission: HomeworkSubmission
) -> ReviewFacts:
    """What a reply needs to know about the review: three reads, no write."""
    task = (
        await session.execute(
            select(AuthoredDocument.task_type, AuthoredDocument.content_hash).where(
                AuthoredDocument.id == submission.authored_document_id
            )
        )
    ).one_or_none()
    later = await session.scalars(
        select(HomeworkSubmission.status).where(
            HomeworkSubmission.student_id == submission.student_id,
            HomeworkSubmission.authored_document_id == submission.authored_document_id,
            HomeworkSubmission.deleted_at.is_(None),
            # "Later" in the attempt ledger's order: by time, the id breaking
            # a tie.
            or_(
                HomeworkSubmission.created_at > submission.created_at,
                and_(
                    HomeworkSubmission.created_at == submission.created_at,
                    HomeworkSubmission.id > submission.id,
                ),
            ),
        )
    )
    task_type = task.task_type if task is not None else None
    return ReviewFacts(
        task_type=AssignmentType(task_type) if task_type is not None else None,
        task_version=task.content_hash if task is not None else None,
        later_statuses=tuple(later),
        items=await review_items(session, submission),
    )


async def review_items(
    session: AsyncSession, submission: HomeworkSubmission
) -> tuple[str, ...]:
    """The remarks of the submission's review: its verdict rows' ids, in order.

    A review repeated by the rule of the same work has no rows of its own;
    its remarks are the rows of the attempt it repeats (module docstring).
    Empty for a review with no verdict rows to stand on — a test, a review of
    today's Mentor, a submission not reviewed.
    """
    verdicts = SubmissionVerdictRepository(session)
    rows: Sequence[SubmissionCriterionVerdict] = await verdicts.list_for_submission(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    if not rows:
        source = await _repeated_attempt(session, submission)
        if source is not None:
            rows = await verdicts.list_for_submission(
                tenant_id=submission.tenant_id, submission_id=source
            )
    return tuple(row.item_id for row in rows)


async def _repeated_attempt(
    session: AsyncSession, submission: HomeworkSubmission
) -> uuid.UUID | None:
    """The attempt whose review this one repeats, or ``None`` for no copy.

    The copy names it by number in its receipt (``dedup_of``); the attempt
    is the student's live attempt at the same task with that number and the
    same work at the same version — the key the copy was given by — and with
    verdict rows (a copy is only ever made of a judged attempt). The latest,
    should the ledger's known limit have given two the same number.
    """
    receipt = submission.attempt_receipt or {}
    number = receipt.get(DEDUP_RECEIPT_KEY)
    if not isinstance(number, int) or submission.fingerprint is None:
        return None
    source: uuid.UUID | None = await session.scalar(
        select(HomeworkSubmission.id)
        .where(
            HomeworkSubmission.tenant_id == submission.tenant_id,
            HomeworkSubmission.student_id == submission.student_id,
            HomeworkSubmission.authored_document_id == submission.authored_document_id,
            HomeworkSubmission.attempt_no == number,
            HomeworkSubmission.fingerprint == submission.fingerprint,
            HomeworkSubmission.task_version.is_not_distinct_from(
                submission.task_version
            ),
            HomeworkSubmission.status.in_(ATTEMPT_STATUSES),
            HomeworkSubmission.deleted_at.is_(None),
            HomeworkSubmission.id != submission.id,
            exists().where(
                SubmissionCriterionVerdict.submission_id == HomeworkSubmission.id,
                SubmissionCriterionVerdict.tenant_id == submission.tenant_id,
            ),
        )
        .order_by(HomeworkSubmission.created_at.desc(), HomeworkSubmission.id.desc())
        .limit(1)
    )
    return source
