"""Where a cost lands in the reports (BIL-C, decision 5).

Requires ``docker compose up -d`` (PostgreSQL), migrated to head.

Two locks, each with the code mutation that turns it red:

* a submission's cost is the cost of EVERY job whose subject it is — red if the
  homework reports join on ``homework_submissions.job_id`` again (each
  continuation overwrites that column, so the earlier jobs' calls drop out);
* a key-explanation job carries its task's course — red if
  ``enqueue_key_explanation`` stops setting ``course_node_id`` (the generation
  then lands in ``/cost/summary``'s unattributed bucket).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.enqueue import enqueue_key_explanation
from course_supporter.jobs import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Tenant,
)
from course_supporter.storage.repositories import (
    CostSums,
    ExternalServiceCallRepository,
    HomeworkCostRepository,
)
from course_supporter.storage.student_repository import StudentRepository

pytestmark = pytest.mark.requires_db

_CALLED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
_FROM = date(2026, 10, 1)
_TO = date(2026, 10, 31)


def _paid_row(job_id: uuid.UUID, cost: str | None, kind: str) -> ExternalServiceCall:
    return ExternalServiceCall(
        job_id=job_id,
        action="review",
        provider="deepseek",
        model_id="deepseek-flash",
        success=True,
        cost_usd=None if cost is None else Decimal(cost),
        cost_kind=kind,
        catalog_version="test-catalog",
        tariff_window="flat",
        created_at=_CALLED_AT,
    )


async def _submission(
    session: AsyncSession, tenant: Tenant, node: CourseNode, task: AuthoredDocument
) -> HomeworkSubmission:
    student = await StudentRepository(session).create(
        tenant_id=tenant.id, external_id=f"ext-{uuid.uuid4().hex[:8]}"
    )
    return await HomeworkRepository(session).create(
        tenant_id=tenant.id,
        student_id=student.id,
        course_node_id=node.id,
        node_id=node.id,
        authored_document_id=task.id,
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
    )


def _homework_job(tenant: Tenant, submission: HomeworkSubmission) -> Job:
    return Job(
        tenant_id=tenant.id,
        job_type=JobType.HOMEWORK_PROCESSING.value,
        subject_type=JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING],
        subject_id=submission.id,
        status="complete",
    )


class TestEveryJobOfASubmissionCounts:
    async def test_the_first_job_and_its_continuation(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        submission = await _submission(
            db_session, seed_tenant, seed_root_node, seed_material_entry
        )
        first = _homework_job(seed_tenant, submission)
        continuation = _homework_job(seed_tenant, submission)
        # An ingestion job of the same tenant: about a document, not a
        # submission — its calls are not homework cost.
        ingestion = Job(
            tenant_id=seed_tenant.id,
            course_node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING.value,
        )
        db_session.add_all([first, continuation, ingestion])
        await db_session.flush()
        # The continuation overwrote the link, as path_continuation does.
        await HomeworkRepository(db_session).set_job_id(submission.id, continuation.id)
        db_session.add_all(
            [
                _paid_row(first.id, "0.30", "exact"),
                _paid_row(continuation.id, "0.20", "estimated"),
                _paid_row(continuation.id, None, "unknown"),
                _paid_row(ingestion.id, "9.99", "exact"),
            ]
        )
        await db_session.flush()

        repo = HomeworkCostRepository(db_session)
        expected = CostSums(exact_usd=0.30, estimated_usd=0.20, unknown_calls=1)

        total = await repo.get_homework_total(
            tenant_id=seed_tenant.id, from_date=_FROM, to_date=_TO
        )
        assert total == pytest.approx(expected)
        by_course = await repo.get_homework_by_course(
            tenant_id=seed_tenant.id, from_date=_FROM, to_date=_TO
        )
        assert [r.course_node_id for r in by_course] == [seed_root_node.id]
        assert [r.cost for r in by_course] == [pytest.approx(expected)]
        by_task = await repo.get_homework_by_task(
            tenant_id=seed_tenant.id,
            course_node_id=seed_root_node.id,
            from_date=_FROM,
            to_date=_TO,
        )
        assert [r.cost for r in by_task] == [pytest.approx(expected)]
        by_student = await repo.get_homework_by_student(
            tenant_id=seed_tenant.id,
            authored_document_id=seed_material_entry.id,
            from_date=_FROM,
            to_date=_TO,
        )
        assert [r.cost for r in by_student] == [pytest.approx(expected)]


class TestKeyExplanationHasACourse:
    async def test_the_job_carries_the_tasks_node_and_reports_find_it(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        redis = AsyncMock()
        redis.enqueue_job = AsyncMock(return_value=None)

        job = await enqueue_key_explanation(
            redis=redis,
            session=db_session,
            tenant_id=seed_tenant.id,
            authored_document_id=seed_material_entry.id,
            reference_id=uuid.uuid4(),
        )

        assert job.course_node_id == seed_material_entry.course_node_id
        db_session.add(_paid_row(job.id, "0.05", "exact"))
        await db_session.flush()

        repo = ExternalServiceCallRepository(db_session)
        by_course = await repo.get_by_course(
            tenant_id=seed_tenant.id, from_date=_FROM, to_date=_TO
        )
        assert [r.course_node_id for r in by_course] == [seed_root_node.id]
        unattributed = await repo.get_unattributed_for_period(
            tenant_id=seed_tenant.id, from_date=_FROM, to_date=_TO
        )
        assert unattributed == CostSums()
