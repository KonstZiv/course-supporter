"""The screen of a student's replies, in the new path's doors (task 12a, K3).

Purpose:
    A submission answers the replies its student wrote to the remarks of the
    previous review (K4). Before any of them is shown to a model that answers,
    each is judged once, here: is it an attempt on the grade or on the check
    itself — hostile, which the safety check decides — and if not, is it about
    the work, which the topic port decides (product owner's decision,
    2026-11-16). What is judged is written on the reply (``topic``,
    ``topic_confidence``, ``hostile_category``); the answering model reads
    those marks, and of a hostile reply the category alone, never its text
    (K4).

    The screen never decides the submission. On the grade only the work bears
    (product owner's decision, 2026-11-16): a reply — a comment, a question, a
    command, a plea — is beside the work, and a hostile one is marked, not
    turned into a refusal of the work it came with.

Where:
    In the doors of the new path (``path_runner._run_path``): after the
    attempt ledger, before the rule of the same work and before the funds
    port and every stage. Before the rule of the same work because a
    submission with replies to answer is never a copy, and the refinement the
    owner named — a copy and a prepared answer when every reply is off-topic
    or hostile (K4) — reads these marks. On every run, a continuation's too:
    only a reply not judged yet is judged, so a run that comes again finds
    nothing to pay for, and one that comes after a reply the last run could
    not judge tries it again.

The two judgements, per reply:
    1. **Hostile?** — the safety check of the path
       (:data:`~course_supporter.homework.path_config.REPLY_SCREEN_STAGE`):
       the same prompt, the same ladder, the same function
       (:func:`~course_supporter.security.stage2.run_stage2_safety_check`),
       shown the course the work is for and what the text screen noticed in
       the reply. Its own call per reply — so a verdict is the verdict of one
       reply — recorded under its own action,
       :data:`~course_supporter.homework.path_config.REPLY_SAFETY_ACTION`, so
       the price of screening replies is read apart from the price of
       screening the work. The verdict makes the reply ``hostile``
       (:func:`is_hostile`) when it is unsafe and names an attempt on the
       grade or the check — ``prompt_injection``, ``policy_violation``,
       ``suspicious_behavior``; the category is the gravest of them
       (:func:`hostile_category`) — or when it is unsafe and names nothing at
       all (hostile, no category). The confidence is the check's.
    2. **About the work?** — the topic port
       (:mod:`~course_supporter.homework.reply_topic`), asked of every reply
       the safety check did not make hostile: one it passed, and one it
       refused for ``off_topic`` alone — being beside the work is not an
       attempt on the grade, and whether a reply is about the work is the
       port's to say. No model reads a text the check found hostile.

    A reply either judgement could not answer (the ladder ran out, the
    verdict did not parse) is left unjudged — no topic — and a later run
    tries it again; the review goes on. The answering model is shown no
    unjudged reply (K4).

Interface:
    :func:`screen_replies` — the screen; :func:`is_hostile` — whether a
    safety verdict makes a reply hostile; :func:`hostile_category` — which
    violation names a hostile reply; :class:`ScreenSummary` — what one run
    judged.

Money:
    The screen's calls are made before the funds port's first question and
    are not reported to it: the shadow estimate and the reserve price the
    path's stages, and the screen is not one. Each call is a row of the
    register with ``action = 'reply_safety'`` and its cost — what screening
    replies costs a submission is the sum of those rows of its job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from course_supporter.homework import remark_comments
from course_supporter.homework.path_config import (
    REPLY_SAFETY_ACTION,
    REPLY_SCREEN_STAGE,
    ModelStage,
)
from course_supporter.homework.path_stages import (
    course_context_of,
    router_stage_config,
)
from course_supporter.homework.remark_comments import (
    RemarkCommentRepository,
    ScreenMark,
    pending_replies,
)
from course_supporter.homework.reply_core import REPLY_SOURCE
from course_supporter.homework.reply_topic import (
    ReplyTopicClassifier,
    build_reply_topic_classifier,
)
from course_supporter.llm.error_categories import LadderExhaustedError
from course_supporter.llm.stage_router import StageExecution
from course_supporter.remark_kinds import CommentTopic
from course_supporter.security.exceptions import (
    SafetyValidationError,
    SecurityRejectedError,
)
from course_supporter.security.policies import HOMEWORK_POLICY
from course_supporter.security.schemas import (
    SafetyResult,
    ScreenFlag,
    ViolationCategory,
)
from course_supporter.security.stage2 import run_stage2_safety_check
from course_supporter.security.text_screen import screen_text

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.path_config import PathConfig
    from course_supporter.homework.remark_comments import CommentRecord
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.models.source import AssignmentType
    from course_supporter.security.schemas import CourseContext
    from course_supporter.storage.orm import HomeworkSubmission

__all__ = ["ScreenSummary", "hostile_category", "is_hostile", "screen_replies"]

logger = structlog.get_logger(__name__)

_BY_GRAVITY: Final = (
    ViolationCategory.PROMPT_INJECTION,
    ViolationCategory.POLICY_VIOLATION,
    ViolationCategory.SUSPICIOUS_BEHAVIOR,
)
"""The violations that make a reply hostile, the gravest first.

A hostile reply is an attempt on the grade or on the check itself (product
owner's decision, 2026-11-16). An attempt on the check outranks everything —
it is what "hostile" is for; then what the platform's policy forbids; then the
taxonomy's catch-all for the clearly malicious. ``off_topic`` is not one of
them: a reply beside the work attempts nothing, and whether it is about the
work is the topic port's to say.
"""


@dataclass(frozen=True, slots=True)
class ScreenSummary:
    """What one run of the screen judged, for the log and the tests."""

    judged: int = 0
    hostile: int = 0
    unjudged: int = 0


def hostile_category(
    violations: Iterable[ViolationCategory],
) -> ViolationCategory | None:
    """The gravest of the named violations that make a reply hostile.

    ``None`` when none of them is named: no violation at all — the check's
    prompt asks for one, and the parser does not insist — or ``off_topic``
    alone (``_BY_GRAVITY``).
    """
    named = frozenset(violations)
    return next((v for v in _BY_GRAVITY if v in named), None)


def is_hostile(verdict: SafetyResult) -> bool:
    """Does the safety check's verdict make the reply hostile?

    * unsafe, naming ``prompt_injection``, ``policy_violation`` or
      ``suspicious_behavior`` — hostile, by the gravest of them;
    * unsafe, naming nothing — hostile, with no category: the check refused
      the text, and the screen does not invent why;
    * unsafe, naming ``off_topic`` alone — not hostile: the reply goes on as
      a safe one does, and the topic port says whether it is about the work;
    * safe — not hostile, whatever it names, as the safety stage of the work
      reads ``is_safe`` alone.
    """
    if verdict.is_safe:
        return False
    return not verdict.violations or hostile_category(verdict.violations) is not None


def reply_safety_execution(config: PathConfig) -> StageExecution:
    """The path's safety check, run under the reply screen's own name.

    The stage description is :data:`REPLY_SCREEN_STAGE`'s, read from the
    file the submission is served by — its prompt, its ladder, its money
    and output ceilings — exactly as the stage itself is run
    (``path_stages._execution``); only the name in the register differs.

    Raises:
        TypeError: the file has no model stage of that name — a file the
            boot refuses for a type that takes replies.
    """
    stage = config.stages.get(REPLY_SCREEN_STAGE)
    if not isinstance(stage, ModelStage):
        msg = f"no model stage {REPLY_SCREEN_STAGE!r} for the reply screen"
        raise TypeError(msg)
    return StageExecution(
        stage=router_stage_config(stage),
        stage_name=REPLY_SAFETY_ACTION,
        stop_on_output_ceiling=True,
        money_ceiling_usd=stage.ceilings.money_usd,
    )


async def screen_replies(
    session: AsyncSession,
    *,
    submission: HomeworkSubmission,
    task_type: AssignmentType,
    router: StageRouter,
    config: PathConfig,
    comments: RemarkCommentRepository | None = None,
    classifier: ReplyTopicClassifier | None = None,
) -> ScreenSummary:
    """Judge the replies ``submission`` is to answer that are not judged yet.

    The replies are :func:`~course_supporter.homework.remark_comments.pending_replies`'
    — none for a type that takes no replies, and then nothing else is read.
    The marks are flushed through ``comments`` for the caller to commit.
    ``comments`` and ``classifier`` default to the production store and
    topic adapter.
    """
    # Through the module, so the one factory a test substitutes is the one
    # every reader of the replies goes through.
    store = (
        comments
        if comments is not None
        else remark_comments.build_remark_comment_repository(session)
    )
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
    unjudged = [reply for reply in pending if reply.topic is None]
    if not unjudged:
        return ScreenSummary()

    execution = reply_safety_execution(config)
    course = await course_context_of(session, submission)
    topics = (
        classifier if classifier is not None else build_reply_topic_classifier(session)
    )
    log = logger.bind(submission_id=str(submission.id))
    judged = hostile = 0
    for reply in unjudged:
        mark = await _judge(
            reply,
            submission=submission,
            router=router,
            execution=execution,
            course=course,
            topics=topics,
            log=log,
        )
        if mark is None:
            continue
        await store.mark_screened(
            tenant_id=submission.tenant_id, comment_id=reply.id, mark=mark
        )
        judged += 1
        hostile += mark.topic is CommentTopic.HOSTILE

    summary = ScreenSummary(
        judged=judged, hostile=hostile, unjudged=len(unjudged) - judged
    )
    log.info(
        "reply_screen_done",
        judged=summary.judged,
        hostile=summary.hostile,
        unjudged=summary.unjudged,
    )
    return summary


async def _judge(
    reply: CommentRecord,
    *,
    submission: HomeworkSubmission,
    router: StageRouter,
    execution: StageExecution,
    course: CourseContext,
    topics: ReplyTopicClassifier,
    log: Any,
) -> ScreenMark | None:
    """One reply's mark, or ``None`` when it could not be judged (logged)."""
    try:
        verdict = await run_stage2_safety_check(
            reply.body,
            router=router,
            course_context=course,
            execution=execution,
            screen_flags=_flags(reply.body),
        )
    except (LadderExhaustedError, SafetyValidationError, SecurityRejectedError):
        log.warning("reply_screen_unjudged", comment_id=str(reply.id), exc_info=True)
        return None
    if is_hostile(verdict):
        category = hostile_category(verdict.violations)
        named = category.value if category is not None else None
        # The category, never the text: the log is read by more people than
        # the review is.
        log.info("reply_screen_hostile", comment_id=str(reply.id), category=named)
        return ScreenMark(
            topic=CommentTopic.HOSTILE,
            topic_confidence=verdict.confidence,
            hostile_category=named,
        )
    if not verdict.is_safe:
        # Refused as off-topic alone: beside the work, not an attempt on it.
        log.info("reply_screen_off_topic_for_safety", comment_id=str(reply.id))
    try:
        topic = await topics.classify(reply, submission=submission)
    except LadderExhaustedError:
        log.warning("reply_screen_unjudged", comment_id=str(reply.id), exc_info=True)
        return None
    return ScreenMark(
        topic=topic.topic, topic_confidence=topic.confidence, hostile_category=None
    )


def _flags(body: str) -> tuple[ScreenFlag, ...]:
    """What the text screen notices in a reply, for the safety check's hint.

    The reply was screened at its door in the same mode and stored in NFC;
    the flags were not stored, and the screen is free, so it runs again here
    — as the comment to a submission is screened again in the worker
    (``doors.screen_student_note``).

    Raises:
        SecurityRejectedError: for a reply the door's screen would refuse
            today — stored before a stricter policy; the reply stays unjudged.
    """
    return screen_text(
        body, name=REPLY_SOURCE, mode=HOMEWORK_POLICY.text_screen_mode
    ).flags
