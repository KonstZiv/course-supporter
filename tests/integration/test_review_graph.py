"""Integration tests for MentorReviewService (sprint-mentor T6).

Requires ``docker compose up -d`` (PostgreSQL).
Run with ``uv run pytest --run-db -v`` against this file.

Exercises the orchestrator against a real DB with a fake agent (no LLM): the
deterministic assembly (config weights -> aggregate -> clamped denoise ->
verdict), the live context build (author notes reach 1A but not course-free 1B),
the history projection (D10), and the dropping of hallucinated prior ids.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.agents.mentor_review import (
    DenoisingOutcome,
    LayerJudgment,
    NodeCourseEvaluation,
    ReconciliationMatch,
)
from course_supporter.homework.review_config import get_mentor_review_config
from course_supporter.homework.review_graph import MentorReviewService
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Student,
    TaskCriteria,
)

pytestmark = pytest.mark.requires_db


class _FakeCriteria:
    """Stands in for CriteriaCacheService — returns a fixed row (or None)."""

    def __init__(self, row: TaskCriteria | None = None) -> None:
        self.row = row
        self.last_id: uuid.UUID | None = None

    async def get_or_compute(
        self, authored_document_id: uuid.UUID
    ) -> TaskCriteria | None:
        self.last_id = authored_document_id
        return self.row


class _FakeAgent:
    """Canned phase outputs; records the kwargs each phase received."""

    def __init__(
        self,
        *,
        delta: int = -3,
        recidivism: list[ReconciliationMatch] | None = None,
    ) -> None:
        self.delta = delta
        self.recidivism = recidivism or []
        self.calls: dict[str, dict[str, Any]] = {}

    async def evaluate_node_course(self, **kw: Any) -> NodeCourseEvaluation:
        self.calls["node_course"] = kw
        return NodeCourseEvaluation(
            node=LayerJudgment(
                score=80, rationale="n", strengths=["a"], weaknesses=["b"]
            ),
            course=LayerJudgment(
                score=70, rationale="c", strengths=[], weaknesses=["w"]
            ),
        )

    async def evaluate_industry(self, **kw: Any) -> LayerJudgment:
        self.calls["industry"] = kw
        return LayerJudgment(score=60, rationale="i", strengths=[], weaknesses=[])

    async def reconcile(self, **kw: Any) -> DenoisingOutcome:
        self.calls["reconcile"] = kw
        return DenoisingOutcome(
            recidivism=self.recidivism,
            corrections=[],
            delta=self.delta,
            summary="history summary",
        )

    async def synthesize(self, **kw: Any) -> str:
        self.calls["synthesize"] = kw
        return "# Review\n\nWell done."


async def _make_student(session: AsyncSession, tenant_id: uuid.UUID) -> Student:
    student = Student(tenant_id=tenant_id, external_id=f"ext-{uuid.uuid4().hex[:8]}")
    session.add(student)
    await session.flush()
    return student


async def _make_submission(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    node_id: uuid.UUID,
    authored_document_id: uuid.UUID,
    status: str = "reviewing",
    review_result: dict[str, Any] | None = None,
    score: int | None = None,
) -> HomeworkSubmission:
    submission = HomeworkSubmission(
        tenant_id=tenant_id,
        student_id=student_id,
        course_node_id=node_id,
        node_id=node_id,
        authored_document_id=authored_document_id,
        file_url="s3://bucket/file.py",
        file_type="text/x-python",
        status=status,
        review_result=review_result,
        score=score,
    )
    session.add(submission)
    await session.flush()
    return submission


def _service(
    session: AsyncSession, agent: _FakeAgent, criteria: _FakeCriteria
) -> MentorReviewService:
    return MentorReviewService(
        session=session,
        agent=agent,  # type: ignore[arg-type]
        criteria_service=criteria,  # type: ignore[arg-type]
        config=get_mentor_review_config(),
    )


class TestAssembly:
    async def test_degrade_path_assembles_valid_result(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student = await _make_student(db_session, seed_root_node.tenant_id)
        submission = await _make_submission(
            db_session,
            tenant_id=seed_root_node.tenant_id,
            student_id=student.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
        )
        agent = _FakeAgent(delta=-3)
        service = _service(db_session, agent, _FakeCriteria(None))

        output = await service.review(
            submission=submission, submission_text="def f(): ...", language="ukr"
        )

        # Config weights applied: 0.5*80 + 0.3*70 + 0.2*60 = 73; denoise -3 -> 70.
        assert output.review_result.aggregate_score == 73
        assert output.score == 70
        assert output.review_result.history_reconciliation.denoised_score == 70
        assert output.review_result.history_reconciliation.denoise_delta == -3
        weights = {layer.layer: layer.weight for layer in output.review_result.layers}
        assert weights == {"node": 0.5, "course": 0.3, "industry": 0.2}
        # 70 -> passed, partially_correct (config bands).
        assert output.review_result.verdict.passed is True
        assert output.review_result.verdict.correctness == "partially_correct"
        assert output.review_markdown == "# Review\n\nWell done."
        # No criteria available -> 1A got an empty list; degrade, no crash.
        assert agent.calls["node_course"]["criteria"] == []

    async def test_denoise_delta_is_clamped_to_cap(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student = await _make_student(db_session, seed_root_node.tenant_id)
        submission = await _make_submission(
            db_session,
            tenant_id=seed_root_node.tenant_id,
            student_id=student.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
        )
        agent = _FakeAgent(delta=50)  # way past the +-10 cap
        service = _service(db_session, agent, _FakeCriteria(None))

        output = await service.review(
            submission=submission, submission_text="x", language="ukr"
        )

        # aggregate 73 + clamp(50, 10) = 83; effective delta stored is +10.
        assert output.score == 83
        assert output.review_result.history_reconciliation.denoise_delta == 10


class TestContextRouting:
    async def test_author_notes_reach_node_course_not_industry(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        seed_material_entry.author_mentor_notes = "weight clarity heavily"
        await db_session.flush()
        student = await _make_student(db_session, seed_root_node.tenant_id)
        submission = await _make_submission(
            db_session,
            tenant_id=seed_root_node.tenant_id,
            student_id=student.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
        )
        agent = _FakeAgent()
        service = _service(db_session, agent, _FakeCriteria(None))

        await service.review(submission=submission, submission_text="x", language="ukr")

        assert (
            agent.calls["node_course"]["author_mentor_notes"]
            == "weight clarity heavily"
        )
        # 1B is course-free: author notes never reach it.
        assert "author_mentor_notes" not in agent.calls["industry"]


class TestHistory:
    async def test_prior_attempt_projected_into_reconcile(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student = await _make_student(db_session, seed_root_node.tenant_id)
        prior = await _make_submission(
            db_session,
            tenant_id=seed_root_node.tenant_id,
            student_id=student.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
            status="completed",
            score=55,
            review_result={
                "layers": [{"layer": "node", "weaknesses": ["off-by-one"]}],
                "verdict": {"correctness": "partially_correct"},
            },
        )
        current = await _make_submission(
            db_session,
            tenant_id=seed_root_node.tenant_id,
            student_id=student.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
        )
        agent = _FakeAgent()
        service = _service(db_session, agent, _FakeCriteria(None))

        await service.review(submission=current, submission_text="x", language="ukr")

        history = agent.calls["reconcile"]["history"]
        assert len(history) == 1
        assert history[0]["submission_id"] == str(prior.id)
        assert history[0]["score"] == 55
        assert history[0]["same_task"] is True
        assert history[0]["weaknesses"] == ["off-by-one"]

    async def test_non_terminal_prior_excluded(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student = await _make_student(db_session, seed_root_node.tenant_id)
        await _make_submission(
            db_session,
            tenant_id=seed_root_node.tenant_id,
            student_id=student.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
            status="reviewing",  # not yet reviewed -> excluded
        )
        current = await _make_submission(
            db_session,
            tenant_id=seed_root_node.tenant_id,
            student_id=student.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
        )
        agent = _FakeAgent()
        service = _service(db_session, agent, _FakeCriteria(None))

        await service.review(submission=current, submission_text="x", language="ukr")

        assert agent.calls["reconcile"]["history"] == []


class TestHallucinationGuard:
    async def test_unknown_prior_id_dropped_from_result(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student = await _make_student(db_session, seed_root_node.tenant_id)
        submission = await _make_submission(
            db_session,
            tenant_id=seed_root_node.tenant_id,
            student_id=student.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
        )
        # No history exists, but the agent invents a recidivism match.
        agent = _FakeAgent(
            recidivism=[
                ReconciliationMatch(
                    signal="x", prior_submission_id="ghost-id", note="n"
                )
            ],
        )
        service = _service(db_session, agent, _FakeCriteria(None))

        output = await service.review(
            submission=submission, submission_text="x", language="ukr"
        )

        # The hallucinated id has no real prior -> dropped from the stored result.
        assert output.review_result.history_reconciliation.recidivism == []
