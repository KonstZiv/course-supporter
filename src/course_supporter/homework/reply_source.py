"""Where the student's replies come from, and where the answers to them go.

Purpose:
    Task 12a, K4. The two stages of the path ``repeat_with_replies`` read the
    threads under the remarks of a task — the student's replies and the
    Mentor's earlier answers — and the explanation keeps an answer for each
    reply. Both sides live in the comments table of K1, which this phase of K4
    does not have yet: the stages depend on :class:`ReplySource` alone, and the
    table is wired in behind it (K4, phase 2).

Interface:
    :class:`ReplySource` — the two methods a stage calls.
    :class:`NotWiredReplySource` — the source until the comments table is
    wired in: no replies, nothing to keep.
    :func:`default_reply_source` — the source the stages use when none is
    handed to them; tests and the measurement hand their own.

Until the table is wired in:
    The path ``repeat_with_replies`` is not reached — no submission state
    selects it before K3 — and if it were, its stages would run as the
    stages without replies run, on their own prompts, with nothing to
    answer: the review is written, the replies wait for the next one. Each
    such read leaves a warning (``reply_source_not_wired``), so a path
    switched on early shows in the log rather than in a student's review.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.student_replies import ThreadEntry
    from course_supporter.homework.verdict_explanation import SettledReplyAnswer
    from course_supporter.storage.orm import HomeworkSubmission

logger = structlog.get_logger(__name__)


class ReplySource(Protocol):
    """The threads a submission's review reads, and where its answers go."""

    async def threads(
        self, session: AsyncSession, submission: HomeworkSubmission
    ) -> tuple[ThreadEntry, ...]:
        """Every entry of every thread under the remarks of the submission's task.

        The student's replies — answered or not — and the Mentor's answers,
        each with its item, kind, topic and time; in any order: the stages
        order them.
        """
        ...

    async def keep_answers(
        self,
        session: AsyncSession,
        submission: HomeworkSubmission,
        answers: Sequence[SettledReplyAnswer],
    ) -> None:
        """Keep the answers of this submission's review, each under its reply.

        In the body's session: they are committed together with the
        checkpoint that marks the explanation done (``impl-rules#12``).
        """
        ...


class NotWiredReplySource:
    """The source before the comments table is wired in (see the module)."""

    async def threads(
        self, session: AsyncSession, submission: HomeworkSubmission
    ) -> tuple[ThreadEntry, ...]:
        logger.warning("reply_source_not_wired", submission_id=str(submission.id))
        return ()

    async def keep_answers(
        self,
        session: AsyncSession,
        submission: HomeworkSubmission,
        answers: Sequence[SettledReplyAnswer],
    ) -> None:
        if answers:
            logger.warning(
                "reply_source_not_wired",
                submission_id=str(submission.id),
                answers=len(answers),
            )


def default_reply_source() -> ReplySource:
    """The source the stages use when none is handed to them."""
    return NotWiredReplySource()
