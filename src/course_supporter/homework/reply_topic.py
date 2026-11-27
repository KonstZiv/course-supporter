"""Is a student's reply about the work? — the port and its adapters (task 12a, K3).

Purpose:
    A reply to a remark is answered by the next review; whether it is about
    the work or about something else decides how (K4). The decision is a
    model's — Gemini Decision 1, chosen by the measurements of 2026-11-17
    (three decision models through OpenRouter: quality and price) and
    2026-11-26 (the same model on Google's own address: the same quality,
    about 0.04 $ per 1 000 replies, about 0.35 s) — and this module is the
    port the reply screen asks, the adapter of that model, and a placeholder
    that answers the same for every reply, for tests.

    Whether a reply is HOSTILE — an attempt on the grade or on the check — is
    not this port's question: the safety check decides it
    (``homework/reply_screen.py``), and a reply it refused never reaches this
    port, so no model reads a text the safety check turned down.

Interface:
    :class:`TopicVerdict` — ``on_topic`` or ``off_topic``, and the model's
    confidence (``None`` when it gives none).
    :class:`ReplyTopicClassifier` — the port (a structural ``Protocol``):
    ``classify(reply, submission=...)``.
    :class:`GeminiDecisionTopic` — the production adapter.
    :class:`EverythingOnTopic` — the placeholder: every reply ``on_topic``,
    confidence ``None`` — "no model judged it", not "judged with certainty".
    Tests of other modules substitute it; production no longer builds it.
    :func:`build_reply_topic_classifier` — the one place the adapter is
    chosen, given the session, the router and the path file.

The adapter:
    One request per reply, through the router, with the stage
    :data:`~course_supporter.homework.path_config.REPLY_TOPIC_STAGE` of
    ``config/submission_paths.yaml`` run under the action
    :data:`~course_supporter.homework.path_config.REPLY_TOPIC_ACTION`: every
    attempt, retried and failed ones too, is a row of the call register with
    its price, apart from the rows of the safety check (``reply_safety``).
    The ladder has one rung, ``gemini_decisions`` / ``gemini-decision-1``
    (``llm/providers/gemini_decisions.py``).

    The questions live in the prompt file (``prompts/reply_topic/v1.md``): the
    two the measurements asked, word for word — ``topic`` (a choice between
    ``on_topic`` and ``off_topic``) and ``influence`` (does the reply try to
    change the grade other than by changing the work). The file renders the
    whole request body; the adapter hands it one value, ``input``.

    ``input`` is one string, exactly as the measurements built it — the labels
    are Ukrainian whatever the language of the course::

        Завдання: «{task title}». {task condition}

        Зауваження рецензії: {what}
        {why}
        {todo}

        Репліка студента: {reply}

    The title and the condition (the task's text) are those the review itself
    reads (``task_context.load_task_context``); a condition longer than
    :data:`TASK_CONDITION_MAX_CHARS` is cut to it, and the log says so with the
    lengths alone. The remark is the one the reply answers —
    ``reply.submission_id`` and ``reply.item_id`` — in the words of that
    review's explanation, what the student read: ``what``, ``why`` and
    ``todo``; a point (``c3.p2``) reads its criterion's remark. An item with no
    remark (a criterion that was met, an explanation that does not read) gives
    an empty remark and the log says so; the question is still asked.

    The verdict is ``topic.choice`` and ``topic.confidence``. ``influence``
    decides nothing — hostility is the safety check's — but its ``noul`` is
    written, with the topic and the confidence, in the line
    ``reply_topic_decided``: numbers and the comment's id, never a text.

    An answer that does not read (not JSON, no ``topic``, a choice that is
    neither label, a confidence outside 0..1) fails the attempt without a
    repeat — asking a decision model again asks the same question — and the
    ladder ends: ``LadderExhaustedError``, as from the router. The screen
    leaves the reply unjudged and a later run tries it again.

Money:
    0.10 $ per 1M input tokens, output free; the price of a reply is its
    input tokens, about a thousand — a tenth of a cent. What is sent to
    Google: the title and the condition of the task, the text of the remark and
    the text of the reply, and nothing else — no names, no identifiers, no
    work of the student.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

import structlog

from course_supporter.homework.explanation_read import read_stored_explanation
from course_supporter.homework.path_config import (
    REPLY_TOPIC_ACTION,
    REPLY_TOPIC_STAGE,
    ModelStage,
)
from course_supporter.homework.path_stages import router_stage_config
from course_supporter.homework.result_builders import ResultNotBuiltError
from course_supporter.homework.task_context import load_task_context
from course_supporter.llm.stage_router import StageExecution
from course_supporter.remark_kinds import CommentTopic
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.path_config import PathConfig
    from course_supporter.homework.remark_comments import CommentRecord
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.storage.orm import HomeworkSubmission

__all__ = [
    "TASK_CONDITION_MAX_CHARS",
    "EverythingOnTopic",
    "GeminiDecisionTopic",
    "ReplyTopicAnswerError",
    "ReplyTopicClassifier",
    "TopicVerdict",
    "build_reply_topic_classifier",
    "reply_topic_execution",
]

logger = structlog.get_logger(__name__)

TASK_CONDITION_MAX_CHARS: Final = 8_000
"""The longest task condition the question carries, in characters.

What the measurements sent; a longer text is cut, not refused, and the log
records the two lengths.
"""

_TOPICS: Final = {
    topic.value: topic for topic in (CommentTopic.ON_TOPIC, CommentTopic.OFF_TOPIC)
}


@dataclass(frozen=True, slots=True)
class TopicVerdict:
    """The port's answer: about the work or not, and how sure.

    Raises:
        ValueError: on ``HOSTILE`` — the safety check's word, never a topic.
    """

    topic: CommentTopic
    confidence: float | None

    def __post_init__(self) -> None:
        if self.topic is CommentTopic.HOSTILE:
            msg = "hostile is the safety check's verdict, not a topic"
            raise ValueError(msg)
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence {self.confidence} is outside 0..1"
            raise ValueError(msg)


class ReplyTopicClassifier(Protocol):
    """The port; the module docstring states the contract."""

    async def classify(
        self, reply: CommentRecord, *, submission: HomeworkSubmission
    ) -> TopicVerdict:
        """Is ``reply`` about the work? ``submission`` is the one answering it."""
        ...


class EverythingOnTopic:
    """The placeholder for tests of the modules that ask the port.

    Every reply is about the work, and no confidence is claimed: the screen
    records ``on_topic`` with an empty confidence, which reads as "judged by
    no model" in every count made from the comments.
    """

    async def classify(
        self, reply: CommentRecord, *, submission: HomeworkSubmission
    ) -> TopicVerdict:
        return TopicVerdict(topic=CommentTopic.ON_TOPIC, confidence=None)


class ReplyTopicAnswerError(ValueError):
    """The decision model's answer does not read.

    Raised from the router's response validator, so the attempt is a paid,
    failed row of the register and the ladder goes on without a repeat. The
    message names the fault, never the answer.
    """


@dataclass(frozen=True, slots=True)
class _Answer:
    """What is read of the model's answers: the verdict and the influence."""

    verdict: TopicVerdict
    influence: float | None


def _read_answers(content: str) -> _Answer:
    """The verdict and the ``influence`` number of the model's ``answers``.

    Raises:
        ReplyTopicAnswerError: the content is not an object with a ``topic``
            answer whose ``choice`` is a topic label and whose ``confidence``,
            if present, is a number in 0..1.
    """
    try:
        answers = json.loads(content)
        topic = answers["topic"]
        choice = topic["choice"]
    except (ValueError, KeyError, TypeError):
        msg = "the answers carry no topic choice"
        raise ReplyTopicAnswerError(msg) from None
    if choice not in _TOPICS:
        msg = "the topic choice is neither on_topic nor off_topic"
        raise ReplyTopicAnswerError(msg)
    confidence = topic.get("confidence")
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not 0.0 <= confidence <= 1.0
    ):
        msg = "the topic confidence is not a number in 0..1"
        raise ReplyTopicAnswerError(msg)
    influence = answers.get("influence")
    noul = influence.get("noul") if isinstance(influence, dict) else None
    return _Answer(
        verdict=TopicVerdict(
            topic=_TOPICS[choice],
            confidence=None if confidence is None else float(confidence),
        ),
        influence=(
            float(noul)
            if isinstance(noul, int | float) and not isinstance(noul, bool)
            else None
        ),
    )


def _validate_answers(content: str) -> None:
    """The router's response validator: an answer that does not read fails."""
    _read_answers(content)


def reply_topic_execution(config: PathConfig) -> StageExecution:
    """The topic stage, run under the reply screen's name for it.

    The stage description is :data:`REPLY_TOPIC_STAGE`'s, read from the file
    the submission is served by — its prompt, its ladder, its money and
    output ceilings — as the stage of a path is run
    (``path_stages._execution``); the name in the register is
    :data:`REPLY_TOPIC_ACTION`.

    Raises:
        TypeError: the file has no model stage of that name — a file the
            boot refuses for a type that takes replies.
    """
    stage = config.stages.get(REPLY_TOPIC_STAGE)
    if not isinstance(stage, ModelStage):
        msg = f"no model stage {REPLY_TOPIC_STAGE!r} for the reply topic"
        raise TypeError(msg)
    return StageExecution(
        stage=router_stage_config(stage),
        stage_name=REPLY_TOPIC_ACTION,
        stop_on_output_ceiling=True,
        money_ceiling_usd=stage.ceilings.money_usd,
    )


class GeminiDecisionTopic:
    """The topic port over Gemini Decision 1, through the router (module docstring)."""

    def __init__(
        self, session: AsyncSession, *, router: StageRouter, config: PathConfig
    ) -> None:
        self._session = session
        self._router = router
        self._execution = reply_topic_execution(config)

    async def classify(
        self, reply: CommentRecord, *, submission: HomeworkSubmission
    ) -> TopicVerdict:
        """Is ``reply`` about the work and the remark it answers?

        Raises:
            LadderExhaustedError: the model gave no answer that reads — the
                screen leaves the reply unjudged.
        """
        title, condition = await self._task(reply, submission)
        remark = await self._remark(reply, submission)
        result = await self._execution.run(
            self._router,
            response_validator=_validate_answers,
            input=_input(title, condition, remark, reply.body),
        )
        answer = _read_answers(result.content)
        logger.info(
            "reply_topic_decided",
            comment_id=str(reply.id),
            topic=answer.verdict.topic.value,
            confidence=answer.verdict.confidence,
            influence=answer.influence,
        )
        return answer.verdict

    async def _task(
        self, reply: CommentRecord, submission: HomeworkSubmission
    ) -> tuple[str, str]:
        """The task's title and condition, the condition cut to its limit."""
        title, _, condition = await load_task_context(
            self._session, submission.authored_document_id
        )
        if len(condition) > TASK_CONDITION_MAX_CHARS:
            logger.info(
                "reply_topic_condition_cut",
                comment_id=str(reply.id),
                chars=len(condition),
                kept=TASK_CONDITION_MAX_CHARS,
            )
            condition = condition[:TASK_CONDITION_MAX_CHARS]
        return title, condition

    async def _remark(
        self, reply: CommentRecord, submission: HomeworkSubmission
    ) -> str:
        """The remark the reply answers, as the student read it; ``""`` if none."""
        stored = await SubmissionVerdictRepository(self._session).get_explanation(
            tenant_id=submission.tenant_id, submission_id=reply.submission_id
        )
        remarks: dict[str, Any] = {}
        if stored is not None:
            try:
                explanation = read_stored_explanation(
                    stored.body,
                    submission_id=reply.submission_id,
                    whose="explanation",
                )
            except ResultNotBuiltError:
                pass  # logged by the reader, without values
            else:
                remarks = {remark.id: remark for remark in explanation.remarks}
        # A point ("c3.p2") is read on its criterion's remark.
        found = remarks.get(reply.item_id) or remarks.get(reply.item_id.split(".")[0])
        if found is None:
            logger.info(
                "reply_topic_remark_missing",
                comment_id=str(reply.id),
                item_id=reply.item_id,
            )
            return ""
        return "\n".join((found.what, found.why, found.todo))


def _input(title: str, condition: str, remark: str, reply: str) -> str:
    """The one string the question is asked about (module docstring)."""
    return (
        f"Завдання: «{title}». {condition}\n\n"
        f"Зауваження рецензії: {remark}\n\n"
        f"Репліка студента: {reply}"
    )


def build_reply_topic_classifier(
    session: AsyncSession, *, router: StageRouter, config: PathConfig
) -> ReplyTopicClassifier:
    """The production adapter; the one place it is chosen.

    Raises:
        TypeError: the file has no :data:`REPLY_TOPIC_STAGE` model stage
            (:func:`reply_topic_execution`).
    """
    return GeminiDecisionTopic(session, router=router, config=config)
