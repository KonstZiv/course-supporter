"""The explanation of a text task's verdicts, asked of a model (task 09b).

A pure LLM transform, like
:class:`~course_supporter.agents.criteria_evaluator.CriteriaEvaluatorAgent`:
no session, no repository, no knowledge of the path. What the facts are and
what an answer must agree with belong to
:mod:`course_supporter.homework.verdict_explanation`; the stage that reads the
verdicts and keeps the answer is :mod:`course_supporter.homework.review_explanation`.
This module renders the request (``prompts/review_explanation/v1.md``), holds
the answer's form and returns what the model said.

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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from course_supporter.agents.wire_schema import wire_schema
from course_supporter.criteria_kinds import VerdictValue
from course_supporter.homework.verdict_explanation import (
    CriterionFacts,
    ExplanationAnswer,
    ExplanationFacts,
    JudgedItem,
    read_explanation,
)
from course_supporter.llm.error_categories import StructuralRetryError

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageExecution, StageRouter

logger = structlog.get_logger(__name__)


RESPONSE_SCHEMA: Final = wire_schema(ExplanationAnswer)
"""The schema of the answer, as the router puts it on the wire (task 09a)."""


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
    """

    task_title: str
    task_description: str
    task_text: str
    facts: ExplanationFacts
    submission_text: str
    language: str


@dataclass(frozen=True, slots=True)
class Explained:
    """What the model said, and which rung of the ladder said it."""

    answer: ExplanationAnswer
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


def render_context(shown: ExplanationInput) -> dict[str, Any]:
    """The prompt's variables for one request.

    Public so the prompt's locks render exactly what a request renders.
    """
    return {
        "task_title": shown.task_title,
        "task_description": shown.task_description,
        "task_text": shown.task_text,
        "result": {"passed": shown.facts.passed, "score": shown.facts.score},
        "criteria": [_criterion_for_prompt(c) for c in shown.facts.criteria],
        "submission_text": shown.submission_text,
        "language": shown.language,
    }


def _criterion_for_prompt(facts: CriterionFacts) -> dict[str, Any]:
    """One criterion with its verdict, as the model reads it.

    A criterion judged on its own carries its verdict's fields; one checked
    by mandatory points carries its points, each with its own.
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
    return shown


def _verdict_for_prompt(item: JudgedItem) -> dict[str, Any]:
    """The fields of one verdict that stand — never the ones that do not.

    A "met" shows the line it stands on and where it is; a "not met" shows
    the sentence on what is missing and whether a quote was not found. The
    quote that was not found is never shown (see
    :class:`~course_supporter.homework.verdict_explanation.JudgedItem`).
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
    return {
        "verdict": item.verdict.value,
        "missing": item.missing,
        "quote_not_found": item.quote_not_found,
    }
