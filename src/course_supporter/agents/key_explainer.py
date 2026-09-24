"""Explanations for a test's answer key (mentor-rebuild tasks 06 and 07).

Writes one explanation per question — why the answer the AUTHOR marked is the
right one — and, from task 07 on, one doubt flag per question: whether the
test's own text says that answer is wrong. A pure LLM transform, like
:class:`~course_supporter.agents.criteria_decomposer.CriteriaDecomposerAgent`
and for the same reason — no session, no repository, no knowledge of versions,
so the compute path is testable by stubbing the :class:`StageRouter`. Deciding
WHETHER to generate is
:class:`~course_supporter.homework.reference_service.ReferenceService`'s job;
persisting the result is the job's.

The model never produces an answer. It is handed the author's answers and
explains them (``TASK.md`` invariant 2): a wrong answer from a model would
become a wrong mark for every student who takes the test, and no amount of
review of the explanations would reveal it. What it may say against a key is a
doubt, and a doubt changes no mark: it hides the model's explanation of that
question from students, nothing more (task 07, invariant 2; ``03-BINDING.md``
4.4, point 6).

Coverage is checked in CODE, not asked for politely. The prompt describes the
shape and the validator enforces it: both fields carry exactly the question
numbers of the key, and every explanation is non-empty. Schema enforcement at
the router level is task 09; until then a violation is a
:class:`StructuralRetryError`, which the router answers with its
instructor-style retry and then with the next rung.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from course_supporter.llm.error_categories import StructuralRetryError

if TYPE_CHECKING:
    from course_supporter.homework.reference_key import AnswerKey
    from course_supporter.llm.stage_router import StageRouter

logger = structlog.get_logger(__name__)

STAGE_NAME: Final = "key_explanation"
"""The stage in ``config/ladders_mentor.yaml`` this agent runs."""

KEY_EXPLANATION_CEILING_USD: Final = 0.08
"""What one attempt at this stage can cost, for the funds port's estimate.

The dearest single attempt across the ladder, from the model registry
(``config/external_services.yaml``, rates read 2026-09-19), against an input of
16 384 tokens — roughly eight times the 2.1k the comparable
``criteria_decomposition`` stage actually used in production on 2026-09-07:

* ``deepseek_thinking/deepseek-v4-pro`` — $0.00066/1k in, $0.00198/1k out,
  32 768-token output ceiling → **$0.0757**;
* ``dashscope/qwen3.7-max`` — $0.00165 / $0.004951, 8 192 → $0.0676;
* ``deepseek/deepseek-v4-flash`` — $0.00022 / $0.00066, 8 192 → $0.0090.

Rounded up to $0.08. It is a ceiling for ONE attempt, the same meaning the path
stages' ``money_usd`` carries (``config/submission_paths.yaml``), not a budget
for the whole ladder: the port is asked once, before the first paid call.

Not calibrated (no spike). The first live run is the data that will correct it;
the rates are the registry's and move with it.
"""


class _KeyExplanationResult(BaseModel):
    """Strict shape of the explanation response: two entries per question.

    ``doubts`` defaults to empty instead of being required. A response without
    it then reaches the coverage check, whose feedback names every question
    that lacks a flag — more use to the retry than "field required", and one
    rule for "absent" and "incomplete" alike.
    """

    model_config = ConfigDict(extra="forbid")

    explanations: dict[str, str]
    doubts: dict[str, bool] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KeyExplanations:
    """What the stage produced for a key.

    ``explanations`` has exactly the key's question numbers. ``doubts`` names
    only the questions the model doubts, each mapped to ``true`` — ``{}`` when
    it doubts none — the shape the version stores and every reader takes.
    """

    explanations: dict[str, str]
    doubts: dict[str, bool]


class KeyExplainerAgent:
    """Turn an author's answer key into one explanation and one doubt per question."""

    def __init__(self, stage_router: StageRouter) -> None:
        self._stage_router = stage_router

    async def explain(
        self,
        *,
        task_text: str,
        answers: AnswerKey,
        language: str | None,
    ) -> KeyExplanations:
        """Explain every answer of the key, and flag the ones the text disputes.

        Args:
            task_text: The test's source text, the one its questions are read
                from.
            answers: The author's answers — question number → right labels.
                They are input, never output: the model explains them.
            language: Human-readable language name (``"Ukrainian"``, not
                ``"ukr"``); ``None`` lets the model follow the test's own
                language.

        Returns:
            One explanation per question of the key, and the doubted ones.

        Raises:
            LadderExhaustedError: The ladder could not produce a valid set
                (propagated; the caller marks the version failed).
        """
        expected = set(answers)
        parsed: dict[str, _KeyExplanationResult] = {}

        def _validator(content: str) -> None:
            try:
                result = _KeyExplanationResult.model_validate_json(content)
            except ValidationError as exc:
                first = exc.errors()[0]
                loc = ".".join(str(x) for x in first.get("loc", []))
                logger.warning(
                    "key_explanation_validation_failed",
                    error_type=first.get("type", "unknown"),
                    error_msg=first.get("msg", ""),
                    error_loc=loc,
                )
                feedback = (
                    f"{first.get('msg', 'validation error')} "
                    f"(field: {loc or '<root>'}). "
                    "Regenerate the response with valid JSON matching the schema."
                )
                raise StructuralRetryError(feedback) from exc

            # Coverage: exactly the key's questions. A missing number would
            # leave a student with no explanation for an answer they got wrong;
            # an extra one would show an explanation for a question that is not
            # in the test. Both pass JSON validation, so the check is here.
            _require_coverage(
                result.explanations,
                expected,
                field="explanations",
                item="explanation",
            )
            for number, text in result.explanations.items():
                if not text.strip():
                    raise StructuralRetryError(
                        f"the explanation for question {number} is empty. "
                        "Every question MUST have a non-empty explanation. "
                        "Regenerate with all of them filled."
                    )
            # The same rule for the doubts: a question without a flag is one
            # the model did not answer about, and reading its silence as "no
            # doubt" would show students an explanation nobody checked.
            _require_coverage(
                result.doubts, expected, field="doubts", item="doubt flag"
            )
            parsed["result"] = result

        await self._stage_router.execute_for_stage(
            STAGE_NAME,
            response_validator=_validator,
            expects_json=True,
            language=language,
            task_text=task_text,
            answers=_answers_for_prompt(answers),
        )
        result = parsed["result"]
        return KeyExplanations(
            explanations=dict(result.explanations),
            doubts={number: True for number, doubt in result.doubts.items() if doubt},
        )


def _require_coverage(
    given: Mapping[str, object], expected: set[str], *, field: str, item: str
) -> None:
    """Refuse a field that does not carry exactly the key's question numbers."""
    got = set(given)
    missing = sorted(expected - got)
    unknown = sorted(got - expected)
    if missing or unknown:
        raise StructuralRetryError(
            f"{field} must cover exactly the questions of the answer key: "
            f"missing {missing}, unexpected {unknown}. Regenerate with one {item} "
            f"for each of {sorted(expected)} and no others."
        )


def _answers_for_prompt(answers: AnswerKey) -> str:
    """Render the key for the prompt: sorted, readable, and unambiguous.

    JSON rather than prose because a label is a single letter, and single
    letters embedded in a sentence invite the model to interpret them rather
    than to quote them back. Sorted numerically so the order the model sees
    matches the order the questions appear in.
    """
    ordered = {
        number: answers[number]
        for number in sorted(
            answers, key=lambda n: (0, int(n)) if n.isdigit() else (1, 0)
        )
    }
    return json.dumps(ordered, ensure_ascii=False, indent=2)
