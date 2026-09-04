"""The Mentor review graph orchestrator (sprint-mentor T6, vision §1369-1400).

Ties the LLM phases (:class:`MentorReviewAgent`) to the deterministic scoring
(:mod:`course_supporter.homework.review_scoring`) and the live context the
graph builds itself (``mentor_context.py`` was removed in T1). The job
(``arq_process_homework``) calls :meth:`MentorReviewService.review` inside
stage ``review`` and persists the returned result.

Flow (ratified decisions 1-7):

1. **Context (live).** Criteria from the T4 cache (``get_or_compute``,
   None-tolerant); the task title/description/text; the node + course
   ``NodeSummaryFinal`` (layers 1 + 2, None-tolerant — empty grounding
   degrades, never hard-fails); ``author_mentor_notes`` (D6); the student's
   prior attempts (D5/D10).
2. **Phase 1 (two LLM calls, concurrent).** 1A node+course (course-grounded),
   1B industry (course-free, anti-anchored).
3. **Phase 2 (code).** Weighted aggregate over the config weights.
4. **Phase 3 (one LLM call + code).** Reconcile vs history → bounded delta;
   the code clamps and owns the final number.
5. **Assemble.** Stored layers (config weights), the D9 ledger (code-derived),
   the code-derived verdict → a validated :class:`ReviewResult`.
6. **Synthesis (one LLM call).** One human review honoring D9.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.agents.mentor_review import (
    LayerJudgment,
    MentorReviewAgent,
)
from course_supporter.homework.criteria_cache import (
    CriteriaCacheService,
    build_criteria_cache_service,
)
from course_supporter.homework.review_config import (
    MentorReviewConfig,
    get_mentor_review_config,
)
from course_supporter.homework.review_scoring import (
    aggregate_score,
    apply_denoise,
    build_score_signals,
    derive_verdict,
)
from course_supporter.homework.task_context import load_task_context
from course_supporter.language import display_name
from course_supporter.models.mentor_review import (
    HistoryReconciliation,
    Layer,
    LayerName,
    ReconciliationItem,
    ReviewResult,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    NodeSummaryFinal,
)

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.storage.orm import HomeworkSubmission

logger = structlog.get_logger(__name__)

_TERMINAL_REVIEWED = {"completed", "delivered"}


@dataclass(frozen=True)
class MentorReviewOutput:
    """What the graph produces for the job to persist."""

    review_result: ReviewResult
    review_markdown: str
    score: int


class MentorReviewService:
    """Runs the three-phase Mentor review graph for one submission."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        agent: MentorReviewAgent,
        criteria_service: CriteriaCacheService,
        config: MentorReviewConfig,
    ) -> None:
        self._session = session
        self._agent = agent
        self._criteria = criteria_service
        self._config = config

    async def review(
        self,
        *,
        submission: HomeworkSubmission,
        submission_text: str,
        language: str | None,
    ) -> MentorReviewOutput:
        """Produce the full review (result + markdown + score) for a submission.

        ``language`` is resolved once per submission by the caller, from the
        student's explicit request, their stored preference and the course
        -- in that order. This method used to make its own two-source
        version of that choice, which is how the sanity gate came to answer
        the same question differently.
        """
        doc = await self._session.get(AuthoredDocument, submission.authored_document_id)
        author_notes = doc.author_mentor_notes if doc else None
        # The model is told the language by name, never by code. Storage is
        # ISO 639-3, so the raw value is ``ukr`` -- which reads as an opaque
        # token to a model that recognises "Ukrainian" reliably. The
        # ingestion family has rendered it this way since 2.4.14; the Mentor
        # was the one circuit that did not.
        language_name = display_name(language) if language else None

        criteria_row = await self._criteria.get_or_compute(
            submission.authored_document_id
        )
        criteria = criteria_row.criteria if criteria_row is not None else []

        task_title, task_description, task_text = await self._load_task_context(
            submission.authored_document_id
        )
        node_summary = self._summary_dict(
            await self._final_for(submission.node_id), include_enclosing=False
        )
        course_summary = self._summary_dict(
            await self._final_for(submission.course_node_id), include_enclosing=True
        )
        history = await self._history(submission)

        # --- Phase 1: node+course (1A) and industry (1B), concurrent + independent.
        node_course, industry = await asyncio.gather(
            self._agent.evaluate_node_course(
                task_title=task_title,
                task_description=task_description,
                task_text=task_text,
                criteria=criteria,
                node_summary=node_summary,
                course_summary=course_summary,
                author_mentor_notes=author_notes,
                submission_text=submission_text,
                language=language_name,
            ),
            self._agent.evaluate_industry(
                task_title=task_title,
                task_description=task_description,
                task_text=task_text,
                submission_text=submission_text,
                language=language_name,
            ),
        )

        layers = [
            self._layer("node", self._config.layer_weights.node, node_course.node),
            self._layer(
                "course", self._config.layer_weights.course, node_course.course
            ),
            self._layer("industry", self._config.layer_weights.industry, industry),
        ]

        # --- Phase 2: weighted aggregate (code, never the LLM).
        aggregate = aggregate_score(layers, self._config.layer_weights)

        # --- Phase 3: denoise vs history (LLM judgment) + code-clamped number.
        denoise = await self._agent.reconcile(
            aggregate_score=aggregate,
            current_layers=[
                {
                    "layer": layer.layer,
                    "score": layer.score,
                    "weaknesses": layer.weaknesses,
                }
                for layer in layers
            ],
            history=history,
            denoise_cap=self._config.denoise.cap,
            language=language_name,
        )
        denoised = apply_denoise(aggregate, denoise.delta, self._config.denoise.cap)
        effective_delta = denoised - aggregate  # the actually-applied (clamped) move

        valid_ids = {entry["submission_id"] for entry in history}
        recidivism = self._reconciliation_items(denoise.recidivism, valid_ids)
        corrections = self._reconciliation_items(denoise.corrections, valid_ids)

        rationales: dict[LayerName, str] = {
            "node": node_course.node.rationale,
            "course": node_course.course.rationale,
            "industry": industry.rationale,
        }
        score_signals = build_score_signals(
            layers,
            layer_rationales=rationales,
            denoise_delta=effective_delta,
            history_reason=denoise.summary,
        )

        review_result = ReviewResult(
            layers=layers,
            aggregate_score=aggregate,
            history_reconciliation=HistoryReconciliation(
                recidivism=recidivism,
                corrections=corrections,
                denoise_delta=effective_delta,
                denoised_score=denoised,
            ),
            score_signals=score_signals,
            verdict=derive_verdict(denoised, self._config.verdict_thresholds),
        )

        # --- Synthesis: one human review honoring D9.
        review_markdown = await self._agent.synthesize(
            layers=[layer.model_dump() for layer in layers],
            aggregate_score=aggregate,
            denoised_score=denoised,
            history_reconciliation=review_result.history_reconciliation.model_dump(),
            score_signals=[signal.model_dump() for signal in score_signals],
            student_note=submission.student_note,
            language=language_name,
        )

        logger.info(
            "mentor_review_complete",
            submission_id=str(submission.id),
            aggregate_score=aggregate,
            denoised_score=denoised,
            has_criteria=bool(criteria),
            history_count=len(history),
        )
        return MentorReviewOutput(
            review_result=review_result,
            review_markdown=review_markdown,
            score=denoised,
        )

    @staticmethod
    def _layer(name: LayerName, weight: float, judgment: LayerJudgment) -> Layer:
        return Layer(
            layer=name,
            weight=weight,
            score=judgment.score,
            strengths=judgment.strengths,
            weaknesses=judgment.weaknesses,
        )

    @staticmethod
    def _reconciliation_items(
        matches: list[Any], valid_ids: set[str]
    ) -> list[ReconciliationItem]:
        """Map agent matches to stored items, dropping hallucinated prior ids."""
        items: list[ReconciliationItem] = []
        for match in matches:
            if match.prior_submission_id not in valid_ids:
                logger.warning(
                    "mentor_review_dropped_unknown_prior",
                    prior_submission_id=match.prior_submission_id,
                )
                continue
            items.append(
                ReconciliationItem(
                    signal=match.signal,
                    prior_submission_id=match.prior_submission_id,
                    note=match.note,
                )
            )
        return items

    async def _final_for(self, course_node_id: uuid.UUID) -> NodeSummaryFinal | None:
        """Active NodeSummaryFinal for a node (None when not generated yet)."""
        stmt = select(NodeSummaryFinal).where(
            NodeSummaryFinal.course_node_id == course_node_id,
            NodeSummaryFinal.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _summary_dict(
        final: NodeSummaryFinal | None, *, include_enclosing: bool
    ) -> dict[str, Any]:
        """Complete render dict for a layer summary (defaults when absent)."""
        base: dict[str, Any] = {
            "title": (final.title or "") if final else "",
            "description": (final.description or "") if final else "",
            "learning_objectives": (final.learning_objectives or []) if final else [],
            "success_criteria": (final.success_criteria or []) if final else [],
            "common_mistakes": (final.common_mistakes or []) if final else [],
        }
        if include_enclosing:
            base["enclosing_context"] = (final.enclosing_context or "") if final else ""
        return base

    async def _load_task_context(
        self, authored_document_id: uuid.UUID
    ) -> tuple[str, str, str]:
        """Task title/description/text (shared loader — see task_context.py)."""
        return await load_task_context(self._session, authored_document_id)

    async def _history(self, submission: HomeworkSubmission) -> list[dict[str, Any]]:
        """Compact projection of the student's prior reviewed attempts (D10).

        Course-wide, all attempts (no compression — DD-4-C), excluding the
        current submission and anything not yet reviewed.
        """
        from course_supporter.storage.homework_repository import HomeworkRepository

        rows = await HomeworkRepository(self._session).get_for_student(
            submission.student_id, course_node_id=submission.course_node_id
        )
        history: list[dict[str, Any]] = []
        for row in rows:
            if row.id == submission.id or row.status not in _TERMINAL_REVIEWED:
                continue
            review_result = row.review_result or {}
            weaknesses: list[str] = []
            for layer in review_result.get("layers", []):
                weaknesses.extend(layer.get("weaknesses", []))
            verdict = review_result.get("verdict", {})
            history.append(
                {
                    "submission_id": str(row.id),
                    "score": row.score if row.score is not None else 0,
                    "correctness": verdict.get("correctness", "unknown"),
                    "same_task": row.authored_document_id
                    == submission.authored_document_id,
                    "weaknesses": weaknesses,
                }
            )
        return history


def build_mentor_review_service(
    session: AsyncSession, stage_router: StageRouter
) -> MentorReviewService:
    """Wire the review graph with the production agent, cache, and config.

    The job (T6) constructs the service from its ``StageRouter`` (ESC rows are
    already tagged with the job id via the ContextVar set at job entry),
    mirroring the criteria-cache and methodist factories.
    """
    return MentorReviewService(
        session=session,
        agent=MentorReviewAgent(stage_router),
        criteria_service=build_criteria_cache_service(session, stage_router),
        config=get_mentor_review_config(),
    )
