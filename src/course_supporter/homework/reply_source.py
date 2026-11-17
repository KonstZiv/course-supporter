"""Where the student's replies come from, and where the answers to them go.

Purpose:
    Task 12a, K4. The two stages of the path ``repeat_with_replies`` read the
    threads under the remarks of a task — the student's replies and the
    Mentor's earlier answers — and the explanation keeps an answer for each
    reply it answers; so does the copy of a review given to the same work
    (``homework/attempt_dedup.py``). Both read and write K1's table through
    K3's store: the replies a review answers are
    :func:`~course_supporter.homework.remark_comments.pending_replies`' — the
    one definition, with the submission's state, the reply screen and the
    rule of the same work — and this module keeps no rule of its own about
    which replies wait for an answer.

Interface:
    :class:`RepliesRead` — the threads as the stages read them, and the
    replies left unjudged.
    :class:`ReplySource` — the port: :meth:`~ReplySource.replies` and
    :meth:`~ReplySource.keep_answers`.
    :class:`CommentsReplySource` — the port over K3's store, the production
    source; :func:`default_reply_source` returns it.
    :func:`reply_phrases` — the phrasebook's words for the answers, in the
    review's language; :func:`course_name` — the course they name.
    :func:`beside_the_work` — whether every reply is judged off the topic or
    hostile; :func:`answer_without_the_model` — the phrasebook's answers to
    such replies, kept as the stage keeps its own (the copy's).

What the stages are shown of the threads:
    Every comment of the student's work at the task's version in force on
    the items of the replies this review answers (K3's
    :class:`~course_supporter.homework.remark_comments.ThreadScope`), oldest
    first; the replies ``pending_replies`` names are marked ``pending``. A
    reply of the student the screen could not judge (no ``topic``) is in no
    thread: no model is shown it, no review answers it now; the ids of the
    pending ones are handed back for the log, never their text.

Keeping the answers:
    :meth:`~ReplySource.keep_answers` hands the store
    (:meth:`~course_supporter.storage.remark_comment_repository.SqlRemarkCommentRepository.answer_replies`)
    each answer under its reply, in the caller's session: the stage's
    answers are committed with the explanation and the checkpoint that marks
    the stage done, or rolled back with them — never half of them. The store
    writes one answer per reply and review: a stage run again rewrites its
    answer in place.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from course_supporter.homework import remark_comments
from course_supporter.homework.remark_comments import (
    CommentRecord,
    MentorAnswer,
    ThreadScope,
    current_task_version,
    pending_replies,
)
from course_supporter.homework.student_replies import ThreadEntry
from course_supporter.homework.task_context import load_task_context
from course_supporter.homework.verdict_explanation import ReplyPhrases
from course_supporter.phrasebook import phrases_for
from course_supporter.remark_kinds import CommentAuthor, CommentTopic
from course_supporter.storage.orm import CourseNode

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.path_config import PathConfig
    from course_supporter.homework.verdict_explanation import SettledReplyAnswer
    from course_supporter.models.source import AssignmentType
    from course_supporter.storage.orm import HomeworkSubmission

logger = structlog.get_logger(__name__)

BESIDE_THE_WORK: Final = frozenset({CommentTopic.OFF_TOPIC, CommentTopic.HOSTILE})
"""The screen's verdicts that say a reply is not about the work."""


@dataclass(frozen=True, slots=True)
class RepliesRead:
    """What a review reads of the replies.

    Attributes:
        entries: The threads of the replies it answers, every judged comment,
            oldest first; the replies it answers are ``pending``.
        unjudged: The ids of the replies ``pending_replies`` names that the
            screen could not judge: shown to no model, answered by no review
            now.
    """

    entries: tuple[ThreadEntry, ...] = ()
    unjudged: tuple[str, ...] = ()


class ReplySource(Protocol):
    """The threads a submission's review reads, and where its answers go."""

    async def replies(
        self,
        session: AsyncSession,
        submission: HomeworkSubmission,
        *,
        task_type: AssignmentType,
        config: PathConfig | None = None,
    ) -> RepliesRead:
        """The threads of the replies the submission answers (the module)."""
        ...

    async def keep_answers(
        self,
        session: AsyncSession,
        submission: HomeworkSubmission,
        answers: Sequence[SettledReplyAnswer],
    ) -> None:
        """Keep the answers of this submission's review, each under its reply.

        In the caller's session, flushed, never committed (the module).
        """
        ...


class CommentsReplySource:
    """The port over K3's store of comments (K1's table) — production's."""

    async def replies(
        self,
        session: AsyncSession,
        submission: HomeworkSubmission,
        *,
        task_type: AssignmentType,
        config: PathConfig | None = None,
    ) -> RepliesRead:
        # Through the module, so the one factory a test substitutes is the
        # one every reader of the replies goes through.
        store = remark_comments.build_remark_comment_repository(session)
        pending = await pending_replies(
            session,
            tenant_id=submission.tenant_id,
            submission_id=submission.id,
            student_id=submission.student_id,
            authored_document_id=submission.authored_document_id,
            task_type=task_type,
            config=config,
            comments=store,
        )
        unjudged = tuple(str(r.id) for r in pending if r.topic is None)
        judged = {r.id: r.item_id for r in pending if r.topic is not None}
        version = await current_task_version(session, submission.authored_document_id)
        if not judged or version is None:
            return RepliesRead(unjudged=unjudged)
        scope = ThreadScope(
            tenant_id=submission.tenant_id,
            student_id=submission.student_id,
            authored_document_id=submission.authored_document_id,
            task_version=version,
        )
        comments = await store.thread(scope, set(judged.values()))
        return RepliesRead(
            entries=tuple(
                _entry(comment, pending=comment.id in judged)
                for comment in comments
                if not _unjudged(comment)
            ),
            unjudged=unjudged,
        )

    async def keep_answers(
        self,
        session: AsyncSession,
        submission: HomeworkSubmission,
        answers: Sequence[SettledReplyAnswer],
    ) -> None:
        if not answers:
            return
        await remark_comments.build_remark_comment_repository(session).answer_replies(
            tenant_id=submission.tenant_id,
            submission_id=submission.id,
            answers=[
                MentorAnswer(reply_id=uuid.UUID(a.comment_id), body=a.answer)
                for a in answers
            ],
        )


def default_reply_source() -> ReplySource:
    """The source the stages use when none is handed to them."""
    return CommentsReplySource()


def _unjudged(comment: CommentRecord) -> bool:
    """A student's reply the screen has not judged: in no thread a model sees."""
    return comment.author is CommentAuthor.STUDENT and comment.topic is None


def _entry(comment: CommentRecord, *, pending: bool) -> ThreadEntry:
    """One comment of the store as a thread's entry."""
    return ThreadEntry(
        comment_id=str(comment.id),
        item_id=comment.item_id,
        author=comment.author,
        kind=comment.kind,
        text=comment.body,
        created_at=comment.created_at,
        parent_id=str(comment.parent_id) if comment.parent_id is not None else None,
        topic=comment.topic,
        hostile_category=comment.hostile_category,
        pending=pending,
    )


def reply_phrases(language: str, course: str) -> ReplyPhrases:
    """The phrasebook's words for the answers, in ``language`` (ISO 639-3).

    ``reply.off_topic.wrap_up`` with ``{course}`` put in: the answer from the
    fourth round off the topic, and the copy's answer to a reply off the
    topic.
    """
    words = phrases_for(language)
    return ReplyPhrases(
        work_only=words["reply.work_only"],
        hostile_fallback=words["reply.hostile.fallback"],
        off_topic_canned=words["reply.off_topic.wrap_up"].replace("{course}", course),
    )


async def course_name(session: AsyncSession, submission: HomeworkSubmission) -> str:
    """The course's name: its root's title, else the task's own."""
    root = await session.get(CourseNode, submission.course_node_id)
    title = root.title.strip() if root is not None and root.title else ""
    if title:
        return title
    task_title, _, _ = await load_task_context(session, submission.authored_document_id)
    return task_title


def beside_the_work(replies: Sequence[CommentRecord]) -> bool:
    """Is every reply judged by the screen as off the topic or hostile?

    ``False`` for none: a submission with no reply has nothing beside the
    work to answer. A reply not judged (no ``topic``) or about the work makes
    it ``False`` — it needs the full check.
    """
    return bool(replies) and all(r.topic in BESIDE_THE_WORK for r in replies)


async def answer_without_the_model(
    session: AsyncSession,
    submission: HomeworkSubmission,
    replies: Sequence[CommentRecord],
    *,
    language: str,
) -> None:
    """The phrasebook's answers to replies beside the work, kept (the copy's).

    A hostile reply — ``reply.hostile.fallback``; one off the topic —
    ``reply.off_topic.wrap_up`` with the course's name: no model is asked.
    Kept as the stage keeps its answers, under each reply, in ``session``.

    Raises:
        ValueError: a reply that is not beside the work (:func:`beside_the_work`)
            — the caller decides on the full check before it comes here.
    """
    if not beside_the_work(replies):
        msg = "only replies beside the work are answered without the model"
        raise ValueError(msg)
    phrases = reply_phrases(language, await course_name(session, submission))
    await remark_comments.build_remark_comment_repository(session).answer_replies(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        answers=[
            MentorAnswer(
                reply_id=reply.id,
                body=(
                    phrases.hostile_fallback
                    if reply.topic is CommentTopic.HOSTILE
                    else phrases.off_topic_canned
                ),
            )
            for reply in replies
        ],
    )
    logger.info(
        "reply_answers_without_the_model",
        submission_id=str(submission.id),
        hostile=sum(1 for r in replies if r.topic is CommentTopic.HOSTILE),
        off_topic=sum(1 for r in replies if r.topic is CommentTopic.OFF_TOPIC),
    )
