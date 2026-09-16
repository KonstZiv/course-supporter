"""Freezing and the three ways a held revision gets going again (task 03).

Criteria 5, 7, 9 and 10. Against real rows because every one of them is about
what the database holds: which job is in flight, what a checkpoint says, and
whether a second job may exist at all. Requires ``docker compose up -d``; run
with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from course_supporter.homework.path_checkpoint import (
    FreezeReason,
    PathCheckpoint,
    save_checkpoint,
)
from course_supporter.homework.path_config import PathKey, SubmissionState
from course_supporter.homework.path_continuation import (
    CONFIGURATION_FREEZES,
    resume_after_top_up,
    resume_orphaned_path_job,
    start_continuation_job,
    sweep_frozen_revisions,
)
from course_supporter.jobs.job_type import JobType
from course_supporter.models.source import AssignmentType
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.requires_db

_KEY = PathKey(AssignmentType.TASK, SubmissionState.FIRST)


@pytest.fixture()
async def held(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """A revision whose job finished, holding a checkpoint frozen on money."""
    async with session_factory() as session:
        tenant = Tenant(name=f"pc-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = CourseNode(
            tenant_id=tenant.id, title="C", order=0, default_language="ukr"
        )
        session.add(node)
        await session.flush()
        doc = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="https://example.com/t",
            task_type="task",
        )
        session.add(doc)
        await session.flush()
        student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:6]}")
        session.add(student)
        await session.flush()
        submission = HomeworkSubmission(
            tenant_id=tenant.id,
            student_id=student.id,
            course_node_id=node.id,
            node_id=node.id,
            authored_document_id=doc.id,
            file_url="s3://b/x.py",
            file_type="text/plain",
            original_filename="x.py",
            delivery_mode="in_app",
            status="received",
        )
        session.add(submission)
        await session.flush()
        job = await JobRepository(session).create(
            job_type=JobType.HOMEWORK_PROCESSING.value,
            tenant_id=tenant.id,
            subject_type="homework_submission",
            subject_id=submission.id,
            input_params={"submission_id": str(submission.id)},
        )
        await session.commit()
        ids = {
            "tenant_id": tenant.id,
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
        await session.execute(
            Job.__table__.delete().where(Job.subject_id == ids["submission_id"])
        )
        await session.commit()


async def _freeze(
    session: AsyncSession,
    ids: dict[str, uuid.UUID],
    reason: FreezeReason,
    *,
    finish_job: bool = True,
) -> None:
    await save_checkpoint(
        session,
        ids["job_id"],
        PathCheckpoint.started(_KEY, ["safety", "attempt_classifier"])
        .with_stage_done("safety")
        .frozen("attempt_classifier", reason),
        current_stage="attempt_classifier",
    )
    if finish_job:
        repo = JobRepository(session)
        await repo.update_status(ids["job_id"], "active")
        await repo.update_status(ids["job_id"], "complete")
        await session.commit()


def _arq() -> MagicMock:
    arq = MagicMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-1"))
    return arq


async def _jobs_of(session: AsyncSession, submission_id: uuid.UUID) -> list[Job]:
    rows = await session.execute(
        select(Job).where(Job.subject_id == submission_id).order_by(Job.queued_at.asc())
    )
    return list(rows.scalars())


class TestStartContinuationJob:
    async def test_a_finished_job_may_be_followed_by_a_new_one(
        self, session_factory: async_sessionmaker[AsyncSession], held: dict
    ) -> None:
        async with session_factory() as session:
            await _freeze(session, held, FreezeReason.STAGE_MONEY_CEILING)
            arq = _arq()

            started = await start_continuation_job(
                session,
                arq,
                tenant_id=held["tenant_id"],
                submission_id=held["submission_id"],
            )

            assert started is True
            arq.enqueue_job.assert_awaited_once()
            assert len(await _jobs_of(session, held["submission_id"])) == 2

    async def test_a_job_in_flight_blocks_a_second_one(
        self, session_factory: async_sessionmaker[AsyncSession], held: dict
    ) -> None:
        """Asked first, because the database would refuse it anyway.

        ``uq_jobs_subject_in_flight`` is a partial unique index on the subject
        while the job is ``queued`` or ``active``. That is exactly why a hold
        ENDS its job: keeping it in flight would make every continuation
        impossible, not merely awkward.
        """
        async with session_factory() as session:
            await _freeze(
                session, held, FreezeReason.STAGE_MONEY_CEILING, finish_job=False
            )
            arq = _arq()

            started = await start_continuation_job(
                session,
                arq,
                tenant_id=held["tenant_id"],
                submission_id=held["submission_id"],
            )

            assert started is False
            arq.enqueue_job.assert_not_awaited()
            assert len(await _jobs_of(session, held["submission_id"])) == 1


class TestTopUp:
    async def test_a_revision_held_for_funds_is_continued(
        self, session_factory: async_sessionmaker[AsyncSession], held: dict
    ) -> None:
        """Criterion 7's second half: the top-up event starts a new job."""
        async with session_factory() as session:
            await _freeze(session, held, FreezeReason.AWAITING_FUNDS)
            repo = JobRepository(session)
            from course_supporter.storage.homework_repository import (
                HomeworkRepository,
            )

            await HomeworkRepository(session).update_status(
                held["submission_id"], "awaiting_funds"
            )
            await session.commit()
            arq = _arq()

            assert (
                await resume_after_top_up(session, arq, held["submission_id"]) is True
            )
            assert len(await _jobs_of(session, held["submission_id"])) == 2
            assert repo is not None

    async def test_a_revision_not_held_for_funds_is_left_alone(
        self, session_factory: async_sessionmaker[AsyncSession], held: dict
    ) -> None:
        async with session_factory() as session:
            await _freeze(session, held, FreezeReason.STAGE_MONEY_CEILING)
            arq = _arq()

            assert (
                await resume_after_top_up(session, arq, held["submission_id"]) is False
            )
            arq.enqueue_job.assert_not_awaited()

    def test_nothing_in_the_codebase_calls_it_yet(self) -> None:
        """By design: the seam exists before the thing that will raise the event.

        A billing adapter is out of this sprint (``SPRINT.md`` section 1), so
        the top-up entry has no production caller — and saying so in a test is
        how the day it gains one becomes a deliberate change rather than a
        detail nobody noticed.
        """
        import course_supporter

        root = Path(course_supporter.__file__).parent
        callers = [
            f"{path.relative_to(root)}:{n}"
            for path in root.rglob("*.py")
            if path.name != "path_continuation.py"
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if "resume_after_top_up" in line
        ]

        assert callers == []


class TestSweepFrozenRevisions:
    @pytest.mark.parametrize(
        "reason", sorted(CONFIGURATION_FREEZES, key=lambda r: r.value)
    )
    async def test_a_configuration_freeze_is_swept(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        held: dict,
        reason: FreezeReason,
    ) -> None:
        """Criterion 9's second half: after the edit, the pass starts a new job."""
        async with session_factory() as session:
            await _freeze(session, held, reason)
        arq = _arq()

        dispatched = await sweep_frozen_revisions(session_factory, arq)

        assert dispatched >= 1
        async with session_factory() as session:
            assert len(await _jobs_of(session, held["submission_id"])) == 2

    async def test_awaiting_funds_is_not_swept(
        self, session_factory: async_sessionmaker[AsyncSession], held: dict
    ) -> None:
        """Money arrives through an account, not through a file."""
        async with session_factory() as session:
            await _freeze(session, held, FreezeReason.AWAITING_FUNDS)
        arq = _arq()

        await sweep_frozen_revisions(session_factory, arq)

        async with session_factory() as session:
            assert len(await _jobs_of(session, held["submission_id"])) == 1

    async def test_todays_mentor_jobs_are_invisible_to_it(
        self, session_factory: async_sessionmaker[AsyncSession], held: dict
    ) -> None:
        """No special case needed: they write no checkpoint at all."""
        async with session_factory() as session:
            repo = JobRepository(session)
            await repo.update_status(held["job_id"], "active")
            await repo.update_status(held["job_id"], "complete")
            await session.commit()
        arq = _arq()

        await sweep_frozen_revisions(session_factory, arq)

        async with session_factory() as session:
            assert len(await _jobs_of(session, held["submission_id"])) == 1

    async def test_a_revision_whose_job_is_in_flight_is_not_doubled(
        self, session_factory: async_sessionmaker[AsyncSession], held: dict
    ) -> None:
        async with session_factory() as session:
            await _freeze(
                session, held, FreezeReason.STAGE_MONEY_CEILING, finish_job=False
            )
        arq = _arq()

        assert await sweep_frozen_revisions(session_factory, arq) == 0
        async with session_factory() as session:
            assert len(await _jobs_of(session, held["submission_id"])) == 1


class TestOrphanedPathJob:
    async def test_a_job_with_a_checkpoint_is_requeued(
        self, session_factory: async_sessionmaker[AsyncSession], held: dict
    ) -> None:
        """Criterion 10: it has work behind it, so it is run again from there."""
        async with session_factory() as session:
            await _freeze(
                session, held, FreezeReason.STAGE_MONEY_CEILING, finish_job=False
            )
            job = await JobRepository(session).get_by_id(held["job_id"])
            assert job is not None
            arq = _arq()

            assert await resume_orphaned_path_job(job, arq, session) is True

            arq.enqueue_job.assert_awaited_once()
            # Re-dispatched, not duplicated: the same job, a fresh ARQ handle.
            assert len(await _jobs_of(session, held["submission_id"])) == 1

    async def test_a_job_without_a_checkpoint_is_left_to_the_sweep(
        self, session_factory: async_sessionmaker[AsyncSession], held: dict
    ) -> None:
        """Every job of today's Mentor is exactly this case."""
        async with session_factory() as session:
            job = await JobRepository(session).get_by_id(held["job_id"])
            assert job is not None
            arq = _arq()

            assert await resume_orphaned_path_job(job, arq, session) is False
            arq.enqueue_job.assert_not_awaited()

    async def test_another_pipelines_job_is_left_to_the_sweep(
        self, session_factory: async_sessionmaker[AsyncSession], held: dict
    ) -> None:
        async with session_factory() as session:
            job = await JobRepository(session).get_by_id(held["job_id"])
            assert job is not None
            # In memory only: the pair (job_type, subject_type) is CHECK-bound
            # in the database, and this asks a question about the guard in the
            # function, not about the row.
            job.job_type = JobType.DOCUMENT_PROCESSING.value
            arq = _arq()

            assert await resume_orphaned_path_job(job, arq, session) is False
