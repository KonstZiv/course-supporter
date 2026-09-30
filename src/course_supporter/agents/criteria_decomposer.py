"""Criteria decomposition agent (sprint-mentor T4, vision §1386 / D11).

Decomposes a task ``AuthoredDocument`` into the discrete, checkable
criteria the Mentor review graph (T6) scores a submission against. This
agent is a pure LLM transform: it takes the already-loaded task material
and returns the criteria list. The lazy read-through caching (content
guard, version-match, persistence) is the
:class:`course_supporter.homework.criteria_cache.CriteriaCacheService`'s
job — keeping this class DB-free makes the compute path unit-testable by
stubbing the :class:`StageRouter`.

Mirrors :class:`course_supporter.agents.methodist.MethodistAgent`: the
stage ladder + prompt live in ``config/ladders_mentor.yaml``
(``criteria_decomposition``) and ``prompts/criteria_decomposition/``;
schema/semantic violations raise :class:`StructuralRetryError` so the
router's instructor-style retry + fallback kicks in; ladder exhaustion
surfaces as :class:`LadderExhaustedError` for the caller to handle.

Two response contracts live here for the length of task 08.
:meth:`CriteriaDecomposerAgent.decompose` answers ``v1.md`` — the
``{statement, evidence}`` pairs today's cache stores.
:meth:`CriteriaDecomposerAgent.compose` answers ``v2.md`` — the form of
:mod:`course_supporter.homework.criteria_form` — and has no caller yet: the
stage keeps one name for both versions (the call register tells them apart
by ``prompt_ref``), and its ladder points at v1 until task 08 switches the
prompt, the list service and the review in one commit (K4), which removes
``decompose`` together with the cache.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated

import structlog
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from course_supporter.homework.criteria_form import (
    MAX_CRITERIA,
    CriteriaComposition,
    CriterionDraft,
    check_methods_for,
    compose_criteria,
)
from course_supporter.llm.error_categories import StructuralRetryError

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter

logger = structlog.get_logger(__name__)

STAGE_NAME = "criteria_decomposition"


class _CriterionItem(BaseModel):
    """One atomic, independently checkable requirement of the task."""

    model_config = ConfigDict(extra="forbid")

    statement: str
    evidence: str


class _CriteriaDecompositionResult(BaseModel):
    """Strict shape of the decomposition LLM response."""

    model_config = ConfigDict(extra="forbid")

    criteria: list[_CriterionItem]


class _CompositionResponse(BaseModel):
    """Strict shape of the v2 response: drafts and contradictions.

    ``contradictions`` defaults to empty for the reason the drafts' lists do
    (:class:`~course_supporter.homework.criteria_form.CriterionDraft`): leaving
    out an empty list says the same as ``[]``.
    """

    model_config = ConfigDict(extra="forbid")

    criteria: Annotated[
        tuple[CriterionDraft, ...], Field(min_length=1, max_length=MAX_CRITERIA)
    ]
    contradictions: tuple[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)], ...
    ] = ()


def _validation_feedback(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(x) for x in first.get("loc", []))
    logger.warning(
        "criteria_decomposition_validation_failed",
        error_type=first.get("type", "unknown"),
        error_msg=first.get("msg", ""),
        error_loc=loc,
    )
    return (
        f"{first.get('msg', 'validation error')} (field: {loc or '<root>'}). "
        "Regenerate the response with valid JSON matching the schema."
    )


class CriteriaDecomposerAgent:
    """Turn a task's material into its cached checkable criteria (D11)."""

    def __init__(self, stage_router: StageRouter) -> None:
        self._stage_router = stage_router

    async def decompose(
        self,
        *,
        task_title: str,
        task_description: str,
        task_text: str,
        task_type: str,
        language: str | None,
    ) -> list[dict[str, str]]:
        """Decompose one task into a non-empty list of checkable criteria.

        Args:
            task_title: The task document's title.
            task_description: The task document's short description.
            task_text: The full task material (concatenated segment content).
            task_type: The assignment type (test/short_task/task/project) —
                steers decomposition granularity, so it is a version key of
                the cache (see CriteriaCacheService).
            language: Human-readable language name for the free-text
                fields (``"Ukrainian"``, not ``"ukr"``); ``None`` lets the
                model follow the task's own language.

        Returns:
            A non-empty list of ``{"statement", "evidence"}`` dicts ready to
            persist as ``TaskCriteria.criteria``.

        Raises:
            LadderExhaustedError: The ladder could not produce a valid
                decomposition (propagated for the caller to handle).
        """
        parsed: dict[str, _CriteriaDecompositionResult] = {}

        def _validator(content: str) -> None:
            try:
                result = _CriteriaDecompositionResult.model_validate_json(content)
            except ValidationError as exc:
                first = exc.errors()[0]
                loc = ".".join(str(x) for x in first.get("loc", []))
                logger.warning(
                    "criteria_decomposition_validation_failed",
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

            # Semantic minimum: a real task decomposes into at least one
            # criterion, and every criterion must carry both non-empty
            # fields. An empty list or a blank field would pass JSON schema
            # validation yet be a useless cache entry.
            if not result.criteria:
                raise StructuralRetryError(
                    "criteria is empty — every assignment decomposes into at "
                    "least one checkable requirement. Regenerate with a "
                    "non-empty criteria list grounded in the assignment text."
                )
            for idx, item in enumerate(result.criteria):
                if not item.statement.strip() or not item.evidence.strip():
                    raise StructuralRetryError(
                        f"criterion {idx} has an empty statement or evidence. "
                        "Every criterion MUST have a non-empty statement and "
                        "non-empty evidence. Regenerate with all fields filled."
                    )
            parsed["result"] = result

        await self._stage_router.execute_for_stage(
            STAGE_NAME,
            response_validator=_validator,
            expects_json=True,
            task_type=task_type,
            language=language,
            task_title=task_title,
            task_description=task_description,
            task_text=task_text,
        )

        result = parsed["result"]
        return [
            {"statement": c.statement, "evidence": c.evidence} for c in result.criteria
        ]

    async def compose(
        self,
        *,
        task_title: str,
        task_description: str,
        task_text: str,
        task_type: str,
        language: str | None,
        node_description: str,
        node_concepts: Sequence[str],
        root_concepts: Sequence[str],
    ) -> CriteriaComposition:
        """Compose one task version's criteria list in the task-08 form (v2).

        The model reads the task in its course (``TASK.md`` 3.2) and writes
        drafts; the code checks them, then assigns the identifiers and drops
        every concept the input did not have
        (:func:`~course_supporter.homework.criteria_form.compose_criteria`).

        Args:
            task_title: The task document's title.
            task_description: The task document's short description.
            task_text: The full task material.
            task_type: The assignment type; it steers the granularity and
                decides whether ``code_test`` is offered and accepted.
            language: Human-readable name of the course language for the
                free-text fields (``"Ukrainian"``, not ``"ukr"``); ``None``
                lets the model follow the task's own language.
            node_description: The description of the task's node, ``""``
                when there is none — contradictions are sought against it.
            node_concepts: The main concepts of the task's node.
            root_concepts: The main concepts of the course root.

        Returns:
            The composition: criteria, contradictions for the author, and the
            count of dropped concepts.

        Raises:
            LadderExhaustedError: The ladder could not produce a valid list
                (propagated for the caller to handle).
        """
        admitted = check_methods_for(task_type)
        parsed: dict[str, _CompositionResponse] = {}

        def _validator(content: str) -> None:
            try:
                response = _CompositionResponse.model_validate_json(content)
            except ValidationError as exc:
                raise StructuralRetryError(_validation_feedback(exc)) from exc
            for index, draft in enumerate(response.criteria):
                if draft.check_method not in admitted:
                    logger.warning(
                        "criteria_decomposition_validation_failed",
                        error_type="check_method_not_admitted",
                        error_msg=draft.check_method.value,
                        error_loc=f"criteria.{index}.check_method",
                    )
                    raise StructuralRetryError(
                        f"criteria.{index}.check_method is "
                        f"{draft.check_method.value!r}, which a {task_type!r} "
                        "assignment does not admit; use one of "
                        f"{sorted(method.value for method in admitted)}. "
                        "Regenerate the response."
                    )
            parsed["response"] = response

        await self._stage_router.execute_for_stage(
            STAGE_NAME,
            response_validator=_validator,
            expects_json=True,
            task_type=task_type,
            language=language,
            task_title=task_title,
            task_description=task_description,
            task_text=task_text,
            node_description=node_description,
            node_concepts=list(node_concepts),
            root_concepts=list(root_concepts),
        )

        response = parsed["response"]
        return compose_criteria(
            response.criteria,
            response.contradictions,
            input_concepts=[*node_concepts, *root_concepts],
        )
