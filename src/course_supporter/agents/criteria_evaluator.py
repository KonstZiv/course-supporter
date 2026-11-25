"""Verdicts on a task's criteria, asked of a model (mentor-rebuild task 09b).

A pure LLM transform, like
:class:`~course_supporter.agents.criteria_decomposer.CriteriaDecomposerAgent`
and for the same reason: no session, no repository, no knowledge of the path —
the compute part is testable by stubbing the router. What to ask about and what
the answers mean belong to the evaluation stage
(:mod:`course_supporter.homework.criteria_evaluation`) and to the pure rules of
:mod:`course_supporter.homework.criteria_verdicts`; this module renders the
request, holds the answer's form, and returns what the model said.

Two requests, one prompt — the one the stage names, rendered from the same
variables: ``prompts/criteria_evaluation/v2.md`` for a text type,
``prompts/criteria_evaluation_project/v1.md`` for a project (chain K4),
``prompts/criteria_evaluation/v2_with_replies.md`` for a text type's
submission that carries the student's replies (task 12a, K4) — which alone
is also shown the replies, in its own slot (:class:`EvaluationInput`). A text
type's FIRST request about a repeated attempt is also shown the earlier
attempt (task 13, K4; :attr:`EvaluationInput.memory`): the earlier verdicts
and the difference of the work, in slots of their own; the second request is
what it was, and so is every request of a first attempt — byte for byte what
v1 and v1_with_replies render, the history v2 is a copy of:

* :meth:`CriteriaEvaluatorAgent.evaluate` asks about every item of the list —
  each criterion, or the mandatory points of one checked by points;
* :meth:`CriteriaEvaluatorAgent.ask_again` asks about the items whose verdict
  cannot stand as given, each with what is wrong with it.

The answer's form is held twice. On the wire, by the strict schema of
:class:`~course_supporter.homework.criteria_verdicts.EvaluationAnswer`, without
its descriptions (:func:`~course_supporter.agents.wire_schema.wire_schema`) —
the router sends the schema itself to a model that holds one and the
provider's JSON mode to one that does not (task 09a). In code, by
:func:`~course_supporter.homework.criteria_verdicts.read_answer`, because a
schema holds the form and never the content: an item missing, unknown or
repeated is a structural retry, which the router answers on the same rung and
then with the next one.

Replacing:
    The stage depends on the two methods and on :class:`EvaluationInput` and
    :class:`Evaluated` alone; another way of asking for verdicts is another
    class with the same two methods.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from course_supporter.agents.wire_schema import wire_schema
from course_supporter.criteria_kinds import VerdictValue
from course_supporter.homework.criteria_form import CheckMethod
from course_supporter.homework.criteria_verdicts import (
    MAX_QUOTE_CHARS,
    MIN_QUOTE_CHARS,
    EvaluationAnswer,
    RepeatItem,
    judged_items,
    read_answer,
)
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.llm.prompt_loader_md import load_prompt

if TYPE_CHECKING:
    import uuid
    from pathlib import Path

    from course_supporter.homework.criteria_form import Criterion
    from course_supporter.homework.earlier_review import (
        AttemptMemory,
        PreviousVerdict,
    )
    from course_supporter.homework.student_replies import EvaluationReply
    from course_supporter.llm.stage_router import StageExecution, StageRouter

logger = structlog.get_logger(__name__)


RESPONSE_SCHEMA: Final = wire_schema(EvaluationAnswer)
"""The schema of the answer, as the router puts it on the wire (task 09a)."""


@dataclass(frozen=True, slots=True)
class EvaluationInput:
    """What the model is shown about one submission.

    Attributes:
        task_title: The task, as the author published it.
        task_description: Its description.
        task_text: Its text.
        criteria: The list in force.
        submission_text: The work exactly as the doors read it — files framed
            by name, and the block of what was not opened. For a project, the
            delta context in its frames (``homework/mentor_context.py``): the
            platform's trusted block, the student's new and changed files,
            the author's neighbouring files.
        language: The course's language by name (``"Ukrainian"``), for the
            sentences on what is missing (``PRE-FLIGHT.md`` 9.9); ``None``
            leaves the model to follow the task's own language.
        replies: The student's replies the stage with replies shows
            (:func:`~course_supporter.homework.student_replies.replies_for_evaluation`;
            task 12a, K4) — possibly none; ``None`` on every other stage,
            whose request has no slot for them and stays what it was.
        memory: What the first request about a repeated attempt of a text
            type is shown of the earlier attempt
            (:func:`~course_supporter.homework.earlier_review.read_attempt_memory`;
            task 13, K4); ``None`` — a first attempt, nothing to show, or a
            stage that shows none: the request is what it was.
    """

    task_title: str
    task_description: str
    task_text: str
    criteria: tuple[Criterion, ...]
    submission_text: str
    language: str | None
    replies: tuple[EvaluationReply, ...] | None = None
    memory: AttemptMemory | None = None


@dataclass(frozen=True, slots=True)
class Evaluated:
    """What the model said, and which rung of the ladder said it.

    The rung is what the repeated request is sent to (``PRE-FLIGHT.md`` 9.5):
    the model whose quotes did not stand is the one asked to mend them.
    ``call_id`` is the register row of the attempt that answered
    (:attr:`StageResult.call_id`), which a verdict names as its source;
    ``None`` when no row was written.
    """

    answer: EvaluationAnswer
    provider: str
    model: str
    call_id: uuid.UUID | None = None


class CriteriaEvaluatorAgent:
    """Asks a model for a verdict on each item of a criteria list."""

    def __init__(self, stage_router: StageRouter) -> None:
        self._stage_router = stage_router

    async def evaluate(
        self, shown: EvaluationInput, *, execution: StageExecution
    ) -> Evaluated:
        """A verdict on every item of the list.

        Raises:
            LadderExhaustedError: No rung gave an answer of the right form
                (propagated; the path decides what that means).
        """
        return await self._ask(
            shown, judged_items(shown.criteria), (), execution=execution
        )

    async def ask_again(
        self,
        shown: EvaluationInput,
        items: Sequence[RepeatItem],
        given: EvaluationAnswer,
        *,
        execution: StageExecution,
    ) -> Evaluated:
        """A verdict once more on ``items`` alone, each told what was wrong.

        ``given`` is the first answer: the model is shown what it said about
        each of these items beside the reason it cannot stand.

        Raises:
            LadderExhaustedError: As :meth:`evaluate`.
        """
        said = {verdict.id: verdict for verdict in given.verdicts}
        notes = [
            {
                "id": item.id,
                "problem": item.feedback,
                "your_answer": said[item.id].model_dump(mode="json", exclude={"id"}),
            }
            for item in items
        ]
        return await self._ask(
            shown, tuple(item.id for item in items), notes, execution=execution
        )

    async def _ask(
        self,
        shown: EvaluationInput,
        expected: tuple[str, ...],
        repeat: Sequence[dict[str, Any]],
        *,
        execution: StageExecution,
    ) -> Evaluated:
        parsed: dict[str, EvaluationAnswer] = {}

        def _validator(content: str) -> None:
            try:
                parsed["answer"] = read_answer(content, expected)
            except StructuralRetryError as refusal:
                logger.warning(
                    "criteria_evaluation_answer_refused", feedback=refusal.feedback
                )
                raise

        result = await execution.run(
            self._stage_router,
            response_validator=_validator,
            expects_json=True,
            response_schema=RESPONSE_SCHEMA,
            **render_context(shown, expected=expected, repeat=repeat),
        )
        return Evaluated(
            answer=parsed["answer"],
            provider=result.provider_used,
            model=result.model_used,
            call_id=result.call_id,
        )


def render_context(
    shown: EvaluationInput,
    *,
    expected: Sequence[str],
    repeat: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """The prompt's variables for one request.

    Public so the prompt's locks render exactly what a request renders.
    ``repeat`` is empty on the first request; on the repeat it names each item
    asked about again, what it said and what is wrong with it. ``replies`` is
    a variable only of the stage with replies (task 12a, K4): every other
    request's variables are what they were. ``previous_attempt`` is a
    variable only of the FIRST request with a memory (task 13, K4;
    :func:`memory_for_prompt`): the repeat asks to mend quotes of THIS work,
    and an earlier work's quotes beside it would only tempt a wrong one; a
    request without the variable renders as before.
    """
    context: dict[str, Any] = {
        "task_title": shown.task_title,
        "task_description": shown.task_description,
        "task_text": shown.task_text,
        "criteria": [_criterion_for_prompt(c) for c in shown.criteria],
        "items": list(expected),
        "repeat": list(repeat),
        "submission_text": shown.submission_text,
        "language": shown.language,
        "max_quote_chars": MAX_QUOTE_CHARS,
        "min_quote_chars": MIN_QUOTE_CHARS,
    }
    if shown.replies is not None:
        context["replies"] = replies_for_prompt(shown.replies)
    if shown.memory is not None and not repeat:
        context["previous_attempt"] = memory_for_prompt(shown.memory)
    return context


def request_chars(
    shown: EvaluationInput,
    *,
    prompt_ref: str,
    expected: Sequence[str],
    base_path: Path | None = None,
) -> int:
    """Characters of the first request as the router renders it: system + user.

    The prompt is read by the loader the router reads it with, from
    ``prompt_ref`` resolved as the router resolves it (``base_path`` ``None``
    — the working directory, as in production), and rendered from
    :func:`render_context`. The measure of the ceiling of the memory's growth
    (task 13, K4; ``homework/earlier_review.py``, ``fit_memory``): the stage
    renders the request with the memory and without it by this one function.
    """
    rendered = load_prompt(prompt_ref, base_path=base_path).render(
        **render_context(shown, expected=expected, repeat=())
    )
    return len(rendered.system or "") + len(rendered.user or "")


def memory_for_prompt(memory: AttemptMemory) -> dict[str, Any]:
    """The earlier attempt as the model reads it (task 13, K4).

    The notes as flags the prompt words; the verdicts — a "met" with its
    quote, a "not met" with its sentence, ``unconfirmed`` only where it holds
    — or ``None`` when not shown; the difference without its last line end
    (the slot's closing tag is on a line of its own), ``None`` when not shown
    or empty: the same work is a note, not an empty slot. Public so the
    measurement renders a request exactly as the stage does.
    """
    return {
        "criteria_changed": memory.criteria_changed,
        "task_changed": memory.task_changed,
        "rewritten": memory.rewritten,
        "work_unavailable": memory.earlier_work_unavailable,
        "work_unchanged": memory.diff == "",
        "verdicts": (
            None
            if memory.verdicts is None
            else [_previous_verdict(v) for v in memory.verdicts]
        ),
        "diff": memory.diff.removesuffix("\n") if memory.diff else None,
    }


def _previous_verdict(verdict: PreviousVerdict) -> dict[str, Any]:
    shown: dict[str, Any] = {"id": verdict.id, "verdict": verdict.verdict.value}
    if verdict.verdict is VerdictValue.MET:
        shown["quote"] = verdict.quote
    else:
        shown["missing"] = verdict.missing
    if verdict.unconfirmed:
        shown["unconfirmed"] = True
    return shown


def replies_for_prompt(replies: Sequence[EvaluationReply]) -> list[dict[str, Any]]:
    """The student's replies as the model reads them: item, kind, text.

    Public so the measurement of the path with replies adds them to a
    recorded request exactly as a request renders them.
    """
    return [
        {"item_id": reply.item_id, "kind": reply.kind.value, "text": reply.text}
        for reply in replies
    ]


def _criterion_for_prompt(criterion: Criterion) -> dict[str, Any]:
    """One criterion as the model reads it: what to check, never how it counts.

    The check method is not shown: the list of items to judge already says
    whether the criterion or its points are judged. The weight is, because a
    reviewer reads "must" and "may" differently.
    """
    shown: dict[str, Any] = {
        "id": criterion.id,
        "weight": criterion.weight.value,
        "text": criterion.text,
        "evidence": criterion.evidence,
    }
    if criterion.check_method is CheckMethod.MANDATORY_POINTS:
        shown["mandatory_points"] = [
            {"id": point.id, "text": point.text} for point in criterion.mandatory_points
        ]
    return shown
