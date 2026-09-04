"""End-to-end integration tests for arq_process_homework (sprint-mentor T7).

Closes the T1 debt: there was no integration test exercising the homework job
orchestration, so the stub era's broken ``reviewing`` transition went uncaught.
This drives the real ``arq_process_homework`` against a live DB with the safety
checks and the sanity/review SERVICES stubbed — the orchestration (status
milestones safety_ok → sanity_ok → reviewing → completed → delivered, the
sanity gate routing, the terminal states, and the webhook emission) is what is
under test here. The sanity agent and the review graph have their own unit /
integration coverage.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_homework_pipeline.py --run-db -v``
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_process_homework
from course_supporter.homework.review_graph import MentorReviewOutput
from course_supporter.homework.sanity_gate import SanityGateOutcome
from course_supporter.models.mentor_review import (
    HistoryReconciliation,
    Layer,
    ReviewResult,
    Verdict,
)
from course_supporter.models.sanity import SanityClassification
from course_supporter.security.schemas import SafetyResult
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)

pytestmark = pytest.mark.requires_db

_WEBHOOK_URL = "https://example.com/homework-hook"


# ── seed ──────────────────────────────────────────────────────────


@pytest.fixture()
async def homework_seed(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Commit tenant + root node + task + student + submission + job.

    Yields the ids; cleans up in reverse FK order afterwards.
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"hw-tenant-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        node = CourseNode(
            tenant_id=tenant.id,
            title="HW Test Course",
            order=0,
            default_language="ukr",
        )
        session.add(node)
        await session.flush()

        task = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="https://example.com/task",
            task_type="task",
        )
        session.add(task)
        await session.flush()

        student = Student(
            tenant_id=tenant.id, external_id=f"stu-{uuid.uuid4().hex[:6]}"
        )
        session.add(student)
        await session.flush()

        submission = HomeworkSubmission(
            tenant_id=tenant.id,
            student_id=student.id,
            course_node_id=node.id,
            node_id=node.id,
            authored_document_id=task.id,
            file_url="s3://bucket/homework/sub.py",
            file_type="text/x-python",
            original_filename="sub.py",
            webhook_url=_WEBHOOK_URL,
            response_language="en",
            status="received",
        )
        session.add(submission)
        await session.flush()

        job = Job(
            tenant_id=tenant.id,
            course_node_id=node.id,
            job_type="homework_processing",
            input_params={"submission_id": str(submission.id)},
            status="queued",
        )
        session.add(job)
        await session.flush()
        await session.commit()

        ids = {
            "tenant_id": tenant.id,
            "node_id": node.id,
            "task_id": task.id,
            "student_id": student.id,
            "submission_id": submission.id,
            "job_id": job.id,
        }

    yield ids

    async with session_factory() as session:
        await session.execute(
            HomeworkSubmission.__table__.delete().where(
                HomeworkSubmission.id == ids["submission_id"]
            )
        )
        await session.execute(Job.__table__.delete().where(Job.id == ids["job_id"]))
        await session.execute(
            Student.__table__.delete().where(Student.id == ids["student_id"])
        )
        await session.execute(
            AuthoredDocument.__table__.delete().where(
                AuthoredDocument.id == ids["task_id"]
            )
        )
        await session.execute(
            CourseNode.__table__.delete().where(CourseNode.id == ids["node_id"])
        )
        await session.execute(
            Tenant.__table__.delete().where(Tenant.id == ids["tenant_id"])
        )
        await session.commit()


# ── stubs ─────────────────────────────────────────────────────────


def _build_ctx(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_file: Path,
) -> dict[str, Any]:
    s3 = MagicMock()
    s3.extract_key = MagicMock(return_value="homework/sub.py")
    s3.download_file = AsyncMock(return_value=tmp_file)
    return {
        "session_factory": session_factory,
        "stage_router": MagicMock(),
        "s3_client": s3,
    }


def _review_output() -> MentorReviewOutput:
    layers = [
        Layer(layer="node", weight=0.5, score=80, strengths=["clear"], weaknesses=[]),
        Layer(layer="course", weight=0.3, score=70, strengths=[], weaknesses=["edge"]),
        Layer(layer="industry", weight=0.2, score=60, strengths=[], weaknesses=[]),
    ]
    review_result = ReviewResult(
        layers=layers,
        aggregate_score=73,
        history_reconciliation=HistoryReconciliation(
            recidivism=[], corrections=[], denoise_delta=0, denoised_score=73
        ),
        score_signals=[],
        verdict=Verdict(passed=True, correctness="partially_correct"),
    )
    return MentorReviewOutput(
        review_result=review_result, review_markdown="Good work.", score=73
    )


class _FakeSanityService:
    def __init__(self, outcome: SanityGateOutcome) -> None:
        self._outcome = outcome

    async def evaluate(
        self, *, submission: Any, submission_text: str, language: str | None = None
    ) -> SanityGateOutcome:
        del submission, submission_text, language
        return self._outcome


class _FakeReviewService:
    def __init__(self, output: MentorReviewOutput) -> None:
        self._output = output

    async def review(
        self, *, submission: Any, submission_text: str, language: str | None = None
    ) -> MentorReviewOutput:
        del submission, submission_text, language
        return self._output


class _RaisingReviewService:
    """A review service that blows up mid-graph (drives the failure path)."""

    async def review(
        self, *, submission: Any, submission_text: str, language: str | None = None
    ) -> Any:
        del submission, submission_text, language
        msg = "mentor review graph exploded"
        raise RuntimeError(msg)


def _patch_pipeline(
    stack: ExitStack,
    *,
    is_safe: bool,
    sanity: SanityGateOutcome,
    review_output: MentorReviewOutput,
    deliver: AsyncMock,
) -> None:
    """Patch the safety functions + the sanity/review service factories."""
    stack.enter_context(
        patch(
            "course_supporter.security.stage1.run_stage1",
            new=MagicMock(
                return_value=SimpleNamespace(
                    archive_entries=None,
                    nfc_text="def f(): return 1",
                    not_opened=(),
                    recovered_encoding="utf-8",
                )
            ),
        )
    )
    stack.enter_context(
        patch(
            "course_supporter.security.stage2.run_stage2_safety_check",
            new=AsyncMock(
                return_value=SafetyResult(
                    is_safe=is_safe,
                    violations=[],
                    confidence=0.95,
                    reasoning="benign" if is_safe else "unsafe content",
                )
            ),
        )
    )
    stack.enter_context(
        patch(
            "course_supporter.homework.sanity_gate.build_sanity_gate_service",
            new=MagicMock(return_value=_FakeSanityService(sanity)),
        )
    )
    stack.enter_context(
        patch(
            "course_supporter.homework.review_graph.build_mentor_review_service",
            new=MagicMock(return_value=_FakeReviewService(review_output)),
        )
    )
    stack.enter_context(
        patch("course_supporter.homework.webhook.deliver_webhook", new=deliver)
    )


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    ids: dict[str, uuid.UUID],
    tmp_path: Path,
    *,
    is_safe: bool = True,
    sanity: SanityGateOutcome | None = None,
    review_output: MentorReviewOutput | None = None,
) -> AsyncMock:
    tmp_file = tmp_path / "sub.py"
    tmp_file.write_text("def f(): return 1", encoding="utf-8")
    ctx = _build_ctx(session_factory, tmp_file)
    deliver = AsyncMock(return_value=True)
    if sanity is None:
        sanity = SanityGateOutcome(
            classification=SanityClassification(
                verdict="match", confidence=0.9, reason="on task"
            ),
            gated=False,
        )
    with ExitStack() as stack:
        _patch_pipeline(
            stack,
            is_safe=is_safe,
            sanity=sanity,
            review_output=review_output or _review_output(),
            deliver=deliver,
        )
        await arq_process_homework(ctx, str(ids["job_id"]), str(ids["submission_id"]))
    return deliver


async def _status(
    session_factory: async_sessionmaker[AsyncSession], sid: uuid.UUID
) -> HomeworkSubmission:
    async with session_factory() as session:
        sub = await session.get(HomeworkSubmission, sid)
        assert sub is not None
        return sub


# ── tests ─────────────────────────────────────────────────────────


class TestHappyPath:
    async def test_full_lifecycle_to_delivered(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        homework_seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        deliver = await _run(session_factory, homework_seed, tmp_path)

        sub = await _status(session_factory, homework_seed["submission_id"])
        assert sub.status == "delivered"
        assert sub.sanity_result == {
            "verdict": "match",
            "confidence": 0.9,
            "reason": "on task",
        }
        assert sub.review_markdown == "Good work."
        assert sub.score == 73
        assert sub.review_result is not None
        assert sub.review_result["verdict"]["passed"] is True

        # reviewed webhook delivered exactly once.
        assert deliver.await_count == 1
        payload = deliver.await_args.kwargs["payload"]
        assert payload.event == "reviewed"
        assert payload.review.score == 73

        async with session_factory() as session:
            job = await session.get(Job, homework_seed["job_id"])
            assert job is not None
            assert job.status == "complete"


class TestSanityMismatch:
    async def test_high_confidence_mismatch_gates_before_review(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        homework_seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        gated = SanityGateOutcome(
            classification=SanityClassification(
                verdict="mismatch", confidence=0.97, reason="answers a different task"
            ),
            gated=True,
        )
        deliver = await _run(session_factory, homework_seed, tmp_path, sanity=gated)

        sub = await _status(session_factory, homework_seed["submission_id"])
        assert sub.status == "mismatch"
        assert sub.sanity_result["verdict"] == "mismatch"
        # The review graph never ran — no score, no review.
        assert sub.score is None
        assert sub.review_result is None
        assert sub.review_markdown is None

        # mismatch webhook delivered (not reviewed).
        assert deliver.await_count == 1
        payload = deliver.await_args.kwargs["payload"]
        assert payload.event == "mismatch"
        assert payload.reason == "answers a different task"


class TestSafetyReject:
    async def test_unsafe_submission_rejected_before_sanity(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        homework_seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        deliver = await _run(session_factory, homework_seed, tmp_path, is_safe=False)

        sub = await _status(session_factory, homework_seed["submission_id"])
        assert sub.status == "rejected"
        # Neither sanity nor review ran.
        assert sub.sanity_result is None
        assert sub.review_result is None
        # No webhook on a safety rejection (not in the T7 event set).
        assert deliver.await_count == 0


class TestProcessingFailure:
    """Рат.2 regress: a mid-pipeline exception drives the body's domain reaction
    (submission → failed + best-effort failed webhook) in a FRESH err-session,
    then the seam writes Job ``failed``. The task does not re-raise out of the
    seam (the seam swallows a terminal failure — no ARQ retry of the body)."""

    async def test_review_failure_marks_failed_and_delivers_webhook(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        homework_seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        tmp_file = tmp_path / "sub.py"
        tmp_file.write_text("def f(): return 1", encoding="utf-8")
        ctx = _build_ctx(session_factory, tmp_file)
        deliver = AsyncMock(return_value=True)
        match = SanityGateOutcome(
            classification=SanityClassification(
                verdict="match", confidence=0.9, reason="on task"
            ),
            gated=False,
        )

        with ExitStack() as stack:
            # Safe + sanity-match so the pipeline reaches the review graph…
            _patch_pipeline(
                stack,
                is_safe=True,
                sanity=match,
                review_output=_review_output(),
                deliver=deliver,
            )
            # …but the review graph blows up mid-run (last patch wins).
            stack.enter_context(
                patch(
                    "course_supporter.homework.review_graph"
                    ".build_mentor_review_service",
                    new=MagicMock(return_value=_RaisingReviewService()),
                )
            )
            # The seam swallows the re-raised exception after writing Job failed.
            await arq_process_homework(
                ctx,
                str(homework_seed["job_id"]),
                str(homework_seed["submission_id"]),
            )

        # Domain reaction (fresh err-session): submission → failed.
        sub = await _status(session_factory, homework_seed["submission_id"])
        assert sub.status == "failed"

        # The seam's terminal: Job → failed.
        async with session_factory() as session:
            job = await session.get(Job, homework_seed["job_id"])
            assert job is not None
            assert job.status == "failed"

        # Best-effort failed webhook delivered exactly once (T7).
        failed_events = [
            c for c in deliver.await_args_list if c.kwargs["payload"].event == "failed"
        ]
        assert len(failed_events) == 1


class TestCancelRaceGuard:
    async def test_soft_deleted_submission_blocks_pipeline_writes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        homework_seed: dict[str, uuid.UUID],
    ) -> None:
        """KD15 §1359 / KD13 §1190 passive guard: a cascade soft-delete makes
        the next pipeline write to the submission fail (the 0.1 BEFORE-UPDATE
        trigger), so an in-flight job cannot complete on a deleted submission.
        """
        sid = homework_seed["submission_id"]

        # Soft-delete the submission (only deleted_at changes — the trigger
        # permits this), simulating a cascade during processing.
        async with session_factory() as session:
            await session.execute(
                text(
                    "UPDATE homework_submissions SET deleted_at = now() WHERE id = :sid"
                ),
                {"sid": sid},
            )
            await session.commit()

        # Any further status write is blocked by the trigger.
        async with session_factory() as session:
            repo = HomeworkRepository(session)
            with pytest.raises(DBAPIError, match="only deleted_at may change"):
                await repo.update_status(sid, "safety_ok")
            await session.rollback()
