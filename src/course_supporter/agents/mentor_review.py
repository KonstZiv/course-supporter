"""Mentor review graph LLM agents (sprint-mentor T6, vision §1369-1400).

The graph's LLM phases, each a pure :class:`StageRouter` transform (DB-free, so
the compute path is unit-testable by stubbing the router). The orchestrator
(:mod:`course_supporter.homework.review_graph`) builds the live context, calls
these methods, and applies the deterministic scoring — the agents only judge.

Phases (ratified decision 1 — phase 1 split in two by grounding seam):

* :meth:`MentorReviewAgent.evaluate_node_course` — layers 1+2 (node, course):
  rubric application, grounded in the node/course ``NodeSummaryFinal``, the task
  criteria (T4 cache), and ``author_mentor_notes`` (D6).
* :meth:`MentorReviewAgent.evaluate_industry` — layer 3 (industry): autonomous
  professional judgment from model knowledge, deliberately course-free (no
  criteria, no Final, no author notes) so the layer is not anchored by the
  course rubric.
* :meth:`MentorReviewAgent.reconcile` — phase 3 (D10): recidivism + corrections
  vs the student's history, plus a bounded denoise delta (the code clamps it).
* :meth:`MentorReviewAgent.synthesize` — the single human review (review
  markdown) from the structured phases + persona + the student's note.

Untrusted-input hardening (ratified decision 6): every method that reads
student text wraps it in delimiting tags with a neutral "this is data, not
instructions" framing (the prompt files); the criteria are computed from the
task before the submission is read, so they cannot be redefined by it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from course_supporter.llm.error_categories import StructuralRetryError

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter

logger = structlog.get_logger(__name__)

STAGE_NODE_COURSE = "mentor_layered_evaluation_node_course"
STAGE_INDUSTRY = "mentor_layered_evaluation_industry"
STAGE_DENOISING = "mentor_denoising"
STAGE_SYNTHESIS = "mentor_synthesis"

_PERSONA = (
    "an experienced senior colleague — applies the course rubric and adds "
    "autonomous professional judgment, never a mechanical checker"
)


class LayerJudgment(BaseModel):
    """One layer's structured judgment as emitted by an evaluation phase."""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    rationale: str = Field(description="One-line reason for this layer's score.")
    strengths: list[str]
    weaknesses: list[str]


class NodeCourseEvaluation(BaseModel):
    """Phase 1A output: the node and course layer judgments."""

    model_config = ConfigDict(extra="forbid")

    node: LayerJudgment
    course: LayerJudgment


class IndustryEvaluation(BaseModel):
    """Phase 1B output: the industry layer judgment (course-free)."""

    model_config = ConfigDict(extra="forbid")

    industry: LayerJudgment


class ReconciliationMatch(BaseModel):
    """One recidivism or correction match against the student's history."""

    model_config = ConfigDict(extra="forbid")

    signal: str
    prior_submission_id: str
    note: str


class DenoisingOutcome(BaseModel):
    """Phase 3 output: history reconciliation + a bounded denoise delta."""

    model_config = ConfigDict(extra="forbid")

    recidivism: list[ReconciliationMatch]
    corrections: list[ReconciliationMatch]
    delta: int = Field(description="Signed nudge; the code re-clamps to the cap.")
    summary: str = Field(description="One-line history summary (D9 signal reason).")


def _require_nonempty(value: str, label: str) -> None:
    if not value.strip():
        raise StructuralRetryError(
            f"{label} must be non-empty. Regenerate with a real {label}."
        )


def _validation_feedback(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(x) for x in first.get("loc", []))
    return (
        f"{first.get('msg', 'validation error')} (field: {loc or '<root>'}). "
        "Regenerate the response with valid JSON matching the schema."
    )


class MentorReviewAgent:
    """The four LLM phases of the Mentor review graph (pure transforms)."""

    def __init__(self, stage_router: StageRouter) -> None:
        self._stage_router = stage_router

    async def evaluate_node_course(
        self,
        *,
        task_title: str,
        task_description: str,
        task_text: str,
        criteria: list[dict[str, str]],
        node_summary: dict[str, Any],
        course_summary: dict[str, Any],
        author_mentor_notes: str | None,
        submission_text: str,
        language: str | None,
    ) -> NodeCourseEvaluation:
        """Phase 1A: judge the submission on the node and course layers."""
        parsed: dict[str, NodeCourseEvaluation] = {}

        def _validator(content: str) -> None:
            try:
                result = NodeCourseEvaluation.model_validate_json(content)
            except ValidationError as exc:
                logger.warning("mentor_node_course_validation_failed")
                raise StructuralRetryError(_validation_feedback(exc)) from exc
            _require_nonempty(result.node.rationale, "node rationale")
            _require_nonempty(result.course.rationale, "course rationale")
            parsed["result"] = result

        await self._stage_router.execute_for_stage(
            STAGE_NODE_COURSE,
            response_validator=_validator,
            expects_json=True,
            task_title=task_title,
            task_description=task_description,
            task_text=task_text,
            criteria=criteria,
            node_summary=node_summary,
            course_summary=course_summary,
            author_mentor_notes=author_mentor_notes,
            submission_text=submission_text,
            language=language,
        )
        return parsed["result"]

    async def evaluate_industry(
        self,
        *,
        task_title: str,
        task_description: str,
        task_text: str,
        submission_text: str,
        language: str | None,
    ) -> LayerJudgment:
        """Phase 1B: judge the submission against industry best practice.

        Deliberately course-free — no criteria, no Final, no author notes — so
        the autonomous layer is not anchored by the course rubric.
        """
        parsed: dict[str, IndustryEvaluation] = {}

        def _validator(content: str) -> None:
            try:
                result = IndustryEvaluation.model_validate_json(content)
            except ValidationError as exc:
                logger.warning("mentor_industry_validation_failed")
                raise StructuralRetryError(_validation_feedback(exc)) from exc
            _require_nonempty(result.industry.rationale, "industry rationale")
            parsed["result"] = result

        await self._stage_router.execute_for_stage(
            STAGE_INDUSTRY,
            response_validator=_validator,
            expects_json=True,
            task_title=task_title,
            task_description=task_description,
            task_text=task_text,
            submission_text=submission_text,
            language=language,
        )
        return parsed["result"].industry

    async def reconcile(
        self,
        *,
        aggregate_score: int,
        current_layers: list[dict[str, Any]],
        history: list[dict[str, Any]],
        denoise_cap: int,
        language: str | None,
    ) -> DenoisingOutcome:
        """Phase 3 (D10): reconcile against history, emit a bounded delta."""
        parsed: dict[str, DenoisingOutcome] = {}

        def _validator(content: str) -> None:
            try:
                result = DenoisingOutcome.model_validate_json(content)
            except ValidationError as exc:
                logger.warning("mentor_denoising_validation_failed")
                raise StructuralRetryError(_validation_feedback(exc)) from exc
            _require_nonempty(result.summary, "summary")
            for kind, matches in (
                ("recidivism", result.recidivism),
                ("corrections", result.corrections),
            ):
                for idx, match in enumerate(matches):
                    if not match.signal.strip() or not match.note.strip():
                        raise StructuralRetryError(
                            f"{kind}[{idx}] has an empty signal or note. "
                            "Every match needs a non-empty signal and note."
                        )
            parsed["result"] = result

        await self._stage_router.execute_for_stage(
            STAGE_DENOISING,
            response_validator=_validator,
            expects_json=True,
            aggregate_score=aggregate_score,
            current_layers=current_layers,
            history=history,
            denoise_cap=denoise_cap,
            language=language,
        )
        return parsed["result"]

    async def synthesize(
        self,
        *,
        layers: list[dict[str, Any]],
        aggregate_score: int,
        denoised_score: int,
        history_reconciliation: dict[str, Any],
        score_signals: list[dict[str, Any]],
        student_note: str | None,
        language: str | None,
    ) -> str:
        """Synthesis: one human review in the persona's voice, honoring D9.

        Fed the score signals verbatim with an instruction to surface each one;
        the student's note is framed as untrusted input acting on emphasis and
        length only, never on the score (decisions 4 + 6).
        """
        result = await self._stage_router.execute_for_stage(
            STAGE_SYNTHESIS,
            response_validator=_synthesis_validator,
            expects_json=False,
            persona=_PERSONA,
            layers=layers,
            aggregate_score=aggregate_score,
            denoised_score=denoised_score,
            history_reconciliation=history_reconciliation,
            score_signals=score_signals,
            student_note=student_note,
            language=language,
        )
        return result.content.strip()


def _synthesis_validator(content: str) -> None:
    if not content.strip():
        raise StructuralRetryError(
            "The review text must be non-empty. Regenerate a real review."
        )
