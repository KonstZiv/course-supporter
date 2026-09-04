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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

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
