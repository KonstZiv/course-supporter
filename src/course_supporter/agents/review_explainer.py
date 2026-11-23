"""The explanation of a text task's verdicts, asked of a model (task 09b).

A pure LLM transform, like
:class:`~course_supporter.agents.criteria_evaluator.CriteriaEvaluatorAgent`:
no session, no repository, no knowledge of the path. What the facts are and
what an answer must agree with belong to
:mod:`course_supporter.homework.verdict_explanation`; the stage that reads the
verdicts and keeps the answer is :mod:`course_supporter.homework.review_explanation`.
This module renders the request — with the prompt its stage names:
``prompts/review_explanation/v4.md`` for a text type,
``prompts/review_explanation_project/v1.md`` for a project (chain K4); the
same variables fill both — holds the answer's form and returns what the model
said.

Since task 10 (K5) the request also shows, for each criterion that is not
met, up to three places of the course the code found for it
(:class:`ShownLink`), each under a local identifier (``c3.l1``); the answer
names the ones it points to. The identifiers the answer was not shown for a
criterion are cut by the stage, not by the check here
(:func:`~course_supporter.homework.verdict_explanation.drop_unshown_links`).

The stage with the student's replies (task 12a, K4) renders
``prompts/review_explanation/v4_with_replies.md``: the same variables and one
more, ``replies`` — the threads under the remarks and the replies to answer
now (:class:`~course_supporter.homework.student_replies.RepliesView`) — and
asks through :meth:`ReviewExplainerAgent.explain_with_replies`, whose answer
has its own form with the answers to the replies. Every other stage renders
and asks exactly what it did.

Since task 13 (explanation side, 2026-11-23) a text type's request — both
of its stages, prompts ``v4.md`` and ``v4_with_replies.md`` — also shows the
limits of examples the code counted from the number of criteria
(:class:`~course_supporter.homework.verdict_explanation.ExampleLimits`):
``result.example_limit`` on every request, and ``result.reply_example_limit``
on the stage with replies; and, beside a criterion whose remark stayed open
since the earlier attempt, ``previous_remark`` — the explanation's own
remark on it then
(:func:`~course_supporter.homework.earlier_review.previous_remarks`). An
input without them — the project's — renders exactly what it did.

The answer is held twice, as the evaluation's is. On the wire, by the strict
schema of :class:`~course_supporter.homework.verdict_explanation.ExplanationAnswer`
without its descriptions (:func:`~course_supporter.agents.wire_schema.wire_schema`).
In code, by :func:`~course_supporter.homework.verdict_explanation.read_explanation`,
because a schema holds the form and never the content: an answer that
repeats the pass wrongly or leaves an unmet criterion without its remark is a
structural retry — the router asks the same rung again with the reason, then
the next one (``PRE-FLIGHT.md`` 9.7, on a failed check).

Replacing:
    The stage depends on :meth:`ReviewExplainerAgent.explain` and on
    :class:`ExplanationInput` and :class:`Explained` alone; another way of
    writing the explanation is another class with the same method.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import structlog

from course_supporter.agents.wire_schema import wire_schema
from course_supporter.criteria_kinds import VerdictValue
from course_supporter.homework.student_replies import AnswerWay
from course_supporter.homework.verdict_explanation import (
    CriterionFacts,
    ExplanationAnswer,
    ExplanationFacts,
    ExplanationWithReplies,
    JudgedItem,
    read_explanation,
    read_explanation_with_replies,
)
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.remark_kinds import CommentTopic

if TYPE_CHECKING:
    from collections.abc import Mapping

    from course_supporter.homework.student_replies import (
        RepliesView,
        ShownAsk,
        ShownEntry,
    )
    from course_supporter.homework.verdict_explanation import (
        ExampleLimits,
        PreviousRemark,
    )
    from course_supporter.llm.stage_router import StageExecution, StageRouter
    from course_supporter.models.review_structure import Position

logger = structlog.get_logger(__name__)


RESPONSE_SCHEMA: Final = wire_schema(ExplanationAnswer)
"""The schema of the answer, as the router puts it on the wire (task 09a)."""

RESPONSE_SCHEMA_WITH_REPLIES: Final = wire_schema(ExplanationWithReplies)
"""The schema of the answer of the stage with replies (task 12a, K4)."""


@dataclass(frozen=True, slots=True)
class ShownLink:
    """One place of the course, as the explanation is shown it (task 10, K5).

    Attributes:
        id: The local identifier the answer names it by: the criterion's id,
            ``.l`` and its rank — ``c3.l1`` is the best place for ``c3``. Not
            a database id: it means something for one request only.
        material: The material's title, as the course author wrote it.
        position: Where in the material the place starts; ``None`` when the
            anchor names none.
        excerpt: The place's first characters; ``None`` when it has no text.
    """

    id: str
    material: str
    position: Position | None
    excerpt: str | None


@dataclass(frozen=True, slots=True)
class ExplanationInput:
    """What the model is shown about one submission.

    Attributes:
        task_title: The task, as the author published it.
        task_description: Its description.
        task_text: Its text.
        facts: The verdicts, the score and the pass, as the code settled them.
        submission_text: The work exactly as the doors read it — files framed
            by name, and the block of what was not opened. Shown so that a
            remark can say how the work does it now (``PRE-FLIGHT.md`` 9.7,
            the input); the facts are what the remark may not contradict.
        language: The language of the explanation by name (``"Ukrainian"``):
            the student's, else the course's (``PRE-FLIGHT.md`` 9.9).
        course_links: The places of the course shown for each criterion that
            is not met, by criterion id; a criterion missing here, or mapped
            to an empty tuple, is shown none.
        replies: The threads and the replies to answer, on the stage with
            replies (task 12a, K4); ``None`` on every other stage, whose
            request has no slot for them and stays what it was.
        example_limits: How many criteria the examples may cover, on a text
            type's stage (task 13); ``None`` on the project's, whose request
            has no field for them and stays what it was.
        previous_remarks: The explanation's own remark at the earlier
            attempt, by criterion id, for each criterion whose remark stayed
            open (task 13); empty when there is none to show.
    """

    task_title: str
    task_description: str
    task_text: str
    facts: ExplanationFacts
    submission_text: str
    language: str
    course_links: Mapping[str, tuple[ShownLink, ...]]
    replies: RepliesView | None = None
    example_limits: ExampleLimits | None = None
    previous_remarks: Mapping[str, PreviousRemark] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Explained:
    """What the model said, and which rung of the ladder said it."""

    answer: ExplanationAnswer
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class ExplainedWithReplies:
    """What the model said on the stage with replies, and which rung said it."""

    answer: ExplanationWithReplies
    provider: str
    model: str


class ReviewExplainerAgent:
    """Asks a model to explain the verdicts on a submission to its student."""

    def __init__(self, stage_router: StageRouter) -> None:
        self._stage_router = stage_router

    async def explain(
        self, shown: ExplanationInput, *, execution: StageExecution
    ) -> Explained:
        """The explanation, agreeing with the facts.

        Raises:
            LadderExhaustedError: No rung gave an answer that agrees with the
                facts (propagated; the path decides what that means).
        """
        parsed: dict[str, ExplanationAnswer] = {}

        def _validator(content: str) -> None:
            try:
                parsed["answer"] = read_explanation(content, shown.facts)
            except StructuralRetryError as refusal:
                logger.warning(
                    "review_explanation_answer_refused", feedback=refusal.feedback
                )
                raise

        result = await execution.run(
            self._stage_router,
            response_validator=_validator,
            expects_json=True,
            response_schema=RESPONSE_SCHEMA,
            **render_context(shown),
        )
        return Explained(
            answer=parsed["answer"],
            provider=result.provider_used,
            model=result.model_used,
        )

    async def explain_with_replies(
        self, shown: ExplanationInput, *, execution: StageExecution
    ) -> ExplainedWithReplies:
        """The explanation, agreeing with the facts, and an answer per reply.

        ``shown.replies`` names the replies to answer; each is answered
        exactly once, or the router asks again (task 12a, K4).

        Raises:
            ValueError: ``shown`` carries no replies — the stage without them
                asks :meth:`explain`.
            LadderExhaustedError: As :meth:`explain`.
        """
        if shown.replies is None:
            msg = "the stage with replies needs the replies it answers"
            raise ValueError(msg)
        to_answer = tuple(ask.id for ask in shown.replies.to_answer)
        parsed: dict[str, ExplanationWithReplies] = {}

        def _validator(content: str) -> None:
            try:
                parsed["answer"] = read_explanation_with_replies(
                    content, shown.facts, to_answer
                )
            except StructuralRetryError as refusal:
                logger.warning(
                    "review_explanation_answer_refused", feedback=refusal.feedback
                )
                raise

        result = await execution.run(
            self._stage_router,
            response_validator=_validator,
            expects_json=True,
            response_schema=RESPONSE_SCHEMA_WITH_REPLIES,
            **render_context(shown),
        )
        return ExplainedWithReplies(
            answer=parsed["answer"],
            provider=result.provider_used,
            model=result.model_used,
        )


def render_context(shown: ExplanationInput) -> dict[str, Any]:
    """The prompt's variables for one request.

    Public so the prompt's locks render exactly what a request renders.
    ``replies`` is a variable only of the stage with replies (task 12a, K4):
    every other request's variables are what they were. The limits of
    examples and the remarks made before (task 13) are fields of ``result``
    and of a criterion, present only when the input carries them.
    """
    context: dict[str, Any] = {
        "task_title": shown.task_title,
        "task_description": shown.task_description,
        "task_text": shown.task_text,
        "result": _result_for_prompt(shown),
        "criteria": [
            _criterion_for_prompt(c, shown.previous_remarks.get(c.criterion.id))
            for c in shown.facts.criteria
        ],
        "submission_text": shown.submission_text,
        "language": shown.language,
        "course_links": [
            {
                "criterion": criterion_id,
                "links": [
                    _link_for_prompt(link)
                    for link in shown.course_links.get(criterion_id, ())
                ],
            }
            for criterion_id in shown.facts.unmet
        ],
    }
    if shown.replies is not None:
        context["replies"] = _replies_for_prompt(shown.replies)
    return context


def _result_for_prompt(shown: ExplanationInput) -> dict[str, Any]:
    """The pass and the score; on a text type's stage, the limits of examples.

    ``reply_example_limit`` goes only with the replies it limits: the stage
    without them has no answer to a reply.
    """
    result: dict[str, Any] = {"passed": shown.facts.passed, "score": shown.facts.score}
    limits = shown.example_limits
    if limits is not None:
        result["example_limit"] = limits.review
        if shown.replies is not None:
            result["reply_example_limit"] = limits.reply
    return result


def _replies_for_prompt(view: RepliesView) -> dict[str, Any]:
    """The threads and the replies to answer, as the model reads them.

    A field without a value is left out, so an entry carries only what
    applies to it. A hostile reply has no text here: the view never holds
    one (:func:`~course_supporter.homework.student_replies.plan_replies`).
    """
    return {
        "threads": [
            {
                "item": thread.item,
                "entries": [_entry_for_prompt(entry) for entry in thread.entries],
            }
            for thread in view.threads
        ],
        "to_answer": [_ask_for_prompt(ask) for ask in view.to_answer],
    }


def _entry_for_prompt(entry: ShownEntry) -> dict[str, Any]:
    """One entry of a thread: a hostile reply by ``hostile`` alone, never text.

    ``hostile`` is the category the safety check named, ``null`` when it
    named none — present on every hostile entry, so the prompt can tell
    "no category" from "not hostile".
    """
    shown: dict[str, Any] = {
        "id": entry.id,
        "author": entry.author.value,
        "kind": entry.kind.value,
        "in_reply_to": entry.in_reply_to,
    }
    if entry.topic is not None:
        shown["topic"] = entry.topic.value
    if entry.text is not None:
        shown["text"] = entry.text
    if entry.topic is CommentTopic.HOSTILE:
        shown["hostile"] = entry.hostile
    return shown


def _ask_for_prompt(ask: ShownAsk) -> dict[str, Any]:
    shown: dict[str, Any] = {"id": ask.id, "how": ask.how.value, "item": ask.item}
    if ask.off_topic_round is not None:
        shown["off_topic_round"] = ask.off_topic_round
    if ask.course is not None:
        shown["course"] = ask.course
    if ask.how is AnswerWay.JOKE:
        shown["hostile"] = ask.hostile
    if ask.style is not None:
        shown["style"] = {
            "id": ask.style.id,
            "description": ask.style.description,
            "example": ask.style.example,
        }
    if ask.work_only is not None:
        shown["work_only"] = ask.work_only
    return shown


def _link_for_prompt(link: ShownLink) -> dict[str, Any]:
    """One place of the course, as the model reads it: the four fields alone."""
    return {
        "id": link.id,
        "material": link.material,
        "position": (
            None
            if link.position is None
            else {"kind": link.position.kind, "value": link.position.value}
        ),
        "excerpt": link.excerpt,
    }


def _criterion_for_prompt(
    facts: CriterionFacts, previous: PreviousRemark | None
) -> dict[str, Any]:
    """One criterion with its verdict, as the model reads it.

    A criterion judged on its own carries its verdict's fields; one checked
    by mandatory points carries its points, each with its own. A criterion
    whose remark stayed open also carries ``previous_remark`` — on the
    criterion, never on a point — and only then (task 13).
    """
    criterion = facts.criterion
    shown: dict[str, Any] = {
        "id": criterion.id,
        "weight": criterion.weight.value,
        "text": criterion.text,
        "evidence": criterion.evidence,
        "verdict": facts.verdict.value,
    }
    if facts.judged is not None:
        shown |= _verdict_for_prompt(facts.judged)
    else:
        texts = {point.id: point.text for point in criterion.mandatory_points}
        shown["points"] = [
            {"id": item.id, "text": texts[item.id], **_verdict_for_prompt(item)}
            for item in facts.points
        ]
    if previous is not None:
        shown["previous_remark"] = {
            "what": previous.what,
            "why": previous.why,
            "todo": previous.todo,
        }
    return shown


def _verdict_for_prompt(item: JudgedItem) -> dict[str, Any]:
    """The fields of one verdict that stand — never the ones that do not.

    A "met" shows the line it stands on and where it is; a "not met" shows
    the sentence on what is missing and whether a quote was not found. The
    quote that was not found is never shown (see
    :class:`~course_supporter.homework.verdict_explanation.JudgedItem`).

    A "not met" that fell for a quote of the author's code also says so,
    ``quote_from_base: true`` (project chain K4) — and only then. Only a
    project has a base, so the field never appears in a text type's request,
    which stays byte for byte what its calibrated prompt was measured on;
    the project's prompt names the field and reads its absence as false.
    This is the one place the two requests differ, and it is a fact of the
    verdict, not a branch on the stage.
    """
    if item.verdict is VerdictValue.MET:
        place = item.place
        return {
            "verdict": item.verdict.value,
            "quote": item.quote,
            "place": (
                None
                if place is None
                else {
                    "file": place.file,
                    "lines": list(place.lines) if place.lines else None,
                }
            ),
            "kept_from_earlier": item.kept_from_earlier,
        }
    shown: dict[str, Any] = {
        "verdict": item.verdict.value,
        "missing": item.missing,
        "quote_not_found": item.quote_not_found,
    }
    if item.quote_from_base:
        shown["quote_from_base"] = True
    return shown
