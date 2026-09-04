"""The sanity gate service (sprint-mentor T7, vision §1336-1339).

Wires the :class:`SanityAgent` classifier to the live task context and the
gate policy (the confidence threshold from ``config/sanity.yaml``). The job
(``arq_process_homework``) builds it between the safety and review stages and
acts on the returned :class:`SanityGateOutcome`.

Gate policy (ratified A3-conservative): a submission is gated (terminal
``mismatch``, review graph skipped) ONLY when the verdict is ``mismatch`` AND
its confidence clears the configured floor. Match, or a low-confidence
mismatch, passes through to the full review unchanged — nothing is injected
into the graph context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.agents.sanity import SanityAgent
from course_supporter.homework.sanity_config import (
    SanityConfig,
    get_sanity_config,
)
from course_supporter.homework.task_context import load_task_context
from course_supporter.language import display_name
from course_supporter.models.sanity import SanityClassification

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.storage.orm import HomeworkSubmission

logger = structlog.get_logger(__name__)


def is_gated(classification: SanityClassification, threshold: float) -> bool:
    """Whether this verdict terminates the pipeline (high-confidence mismatch).

    Pure decision (no I/O): a ``mismatch`` whose confidence reaches the floor.
    A ``match`` (any confidence) or a sub-threshold ``mismatch`` passes through.
    """
    return classification.verdict == "mismatch" and (
        classification.confidence >= threshold
    )


@dataclass(frozen=True)
class SanityGateOutcome:
    """What the gate produces for the job to act on."""

    classification: SanityClassification
    gated: bool


class SanityGateService:
    """Runs the sanity classifier and applies the gate policy for one submission."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        agent: SanityAgent,
        config: SanityConfig,
    ) -> None:
        self._session = session
        self._agent = agent
        self._config = config

    async def evaluate(
        self,
        *,
        submission: HomeworkSubmission,
        submission_text: str,
        language: str | None,
    ) -> SanityGateOutcome:
        """Classify the submission and decide whether it is gated.

        ``language`` is resolved once per submission by the caller. It used
        to be read here as ``submission.response_language`` alone, with no
        fallback -- and since no interface sends that field, the classifier
        was asked to judge in no language at all on every real submission
        while the review two stages later used the course's.
        """
        task_title, task_description, task_text = await load_task_context(
            self._session, submission.authored_document_id
        )
        classification = await self._agent.classify(
            task_title=task_title,
            task_description=task_description,
            task_text=task_text,
            submission_text=submission_text,
            language=display_name(language) if language else None,
        )
        gated = is_gated(classification, self._config.confidence_threshold)
        logger.info(
            "homework_sanity_evaluated",
            submission_id=str(submission.id),
            verdict=classification.verdict,
            confidence=classification.confidence,
            gated=gated,
        )
        return SanityGateOutcome(classification=classification, gated=gated)


def build_sanity_gate_service(
    session: AsyncSession, stage_router: StageRouter
) -> SanityGateService:
    """Wire the sanity gate with the production agent and config.

    Mirrors :func:`build_mentor_review_service` — the job constructs the
    service from its ``StageRouter`` (ESC rows already tagged with the job id).
    """
    return SanityGateService(
        session=session,
        agent=SanityAgent(stage_router),
        config=get_sanity_config(),
    )
