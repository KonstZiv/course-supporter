"""Verdicts on a task's criteria, asked of a model (mentor-rebuild task 09b).

A pure LLM transform, like
:class:`~course_supporter.agents.criteria_decomposer.CriteriaDecomposerAgent`
and for the same reason: no session, no repository, no knowledge of the path —
the compute part is testable by stubbing the router. What to ask about and what
the answers mean belong to the evaluation stage
(:mod:`course_supporter.homework.criteria_evaluation`) and to the pure rules of
:mod:`course_supporter.homework.criteria_verdicts`; this module renders the
request, holds the answer's form, and returns what the model said.

Two requests, one prompt (``prompts/criteria_evaluation/v1.md``):

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

if TYPE_CHECKING:
    from course_supporter.homework.criteria_form import Criterion
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
            by name, and the block of what was not opened.
        language: The course's language by name (``"Ukrainian"``), for the
            sentences on what is missing (``PRE-FLIGHT.md`` 9.9); ``None``
            leaves the model to follow the task's own language.
    """

    task_title: str
    task_description: str
    task_text: str
    criteria: tuple[Criterion, ...]
    submission_text: str
    language: str | None


@dataclass(frozen=True, slots=True)
class Evaluated:
    """What the model said, and which rung of the ladder said it.

    The rung is what the repeated request is sent to (``PRE-FLIGHT.md`` 9.5):
    the model whose quotes did not stand is the one asked to mend them.
    """

    answer: EvaluationAnswer
    provider: str
    model: str


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
    asked about again, what it said and what is wrong with it.
    """
    return {
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
