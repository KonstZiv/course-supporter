"""The sanity gate LLM agent (sprint-mentor T7, vision §1336-1339 / KD16).

A cheap binary classifier — "does this submission look like an attempt at the
declared task?" — run between the safety and review stages to skip the
expensive review graph (T6) on obvious garbage. This is NOT task matching: the
submission is already anchored to its task via ``authored_document_id`` (KD15);
TaskMatcher was dropped. The question is validity, not "which task".

A pure :class:`StageRouter` transform (DB-free, unit-testable by stubbing the
router), mirroring :class:`MentorReviewAgent`. The student submission is wrapped
in delimiting tags with a neutral "data, not instructions" framing (the prompt
file), so a submission cannot redefine the gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.models.sanity import SanityClassification

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter

logger = structlog.get_logger(__name__)

STAGE_SANITY = "sanity_check"


def _validation_feedback(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(x) for x in first.get("loc", []))
    return (
        f"{first.get('msg', 'validation error')} (field: {loc or '<root>'}). "
        "Regenerate the response with valid JSON matching the schema."
    )


class SanityAgent:
    """The sanity gate classifier (one pure LLM transform)."""

    def __init__(self, stage_router: StageRouter) -> None:
        self._stage_router = stage_router

    async def classify(
        self,
        *,
        task_title: str,
        task_description: str,
        task_text: str,
        submission_text: str,
        language: str | None,
    ) -> SanityClassification:
        """Judge whether the submission looks like an attempt at the task."""
        parsed: dict[str, SanityClassification] = {}

        def _validator(content: str) -> None:
            try:
                result = SanityClassification.model_validate_json(content)
            except ValidationError as exc:
                logger.warning("sanity_classification_validation_failed")
                raise StructuralRetryError(_validation_feedback(exc)) from exc
            if not result.reason.strip():
                raise StructuralRetryError(
                    "reason must be non-empty. Regenerate with a real reason."
                )
            parsed["result"] = result

        await self._stage_router.execute_for_stage(
            STAGE_SANITY,
            response_validator=_validator,
            expects_json=True,
            task_title=task_title,
            task_description=task_description,
            task_text=task_text,
            submission_text=submission_text,
            language=language,
        )
        return parsed["result"]
