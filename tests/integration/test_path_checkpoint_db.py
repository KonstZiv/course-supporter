"""The checkpoint against real rows: durability and reading the latest job.

Both properties are about the database and nothing else — that a saved
checkpoint survives a rollback, and that a continuation reads the job that
recorded the work rather than the one it happens to hold. Requires
``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import IntegrityError

from course_supporter.homework.path_checkpoint import (
    FreezeReason,
    PathCheckpoint,
    StageState,
    load_checkpoint,
    save_checkpoint,
)
from course_supporter.homework.path_config import PathKey, SubmissionState
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.models.source import AssignmentType
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import Job, Tenant

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.requires_db

_KEY = PathKey(AssignmentType.TASK, SubmissionState.FIRST)
_SUBJECT = JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING]


async def _job(session: AsyncSession, tenant: Tenant, submission_id: uuid.UUID) -> Job:
    return await JobRepository(session).create(
        job_type=JobType.HOMEWORK_PROCESSING.value,
        tenant_id=tenant.id,
        subject_type=_SUBJECT,
        subject_id=submission_id,
        input_params={"submission_id": str(submission_id)},
    )


class TestDurability:
    async def test_a_saved_checkpoint_survives_a_rollback(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """Committed, not flushed: a continuation must not re-run a paid stage."""
        submission_id = uuid.uuid4()
        job = await _job(db_session, seed_tenant, submission_id)
        await db_session.commit()
        cp = PathCheckpoint.started(_KEY, ["safety", "attempt_classifier"])

        await save_checkpoint(
            db_session, job.id, cp.with_stage_done("safety"), current_stage="safety"
        )
        await db_session.rollback()

        back = await load_checkpoint(db_session, submission_id)
        assert back is not None
        assert back.stages["safety"] is StageState.DONE

    async def test_the_current_stage_goes_to_its_own_column(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """One fact, one place — it is not mirrored inside the JSON."""
        submission_id = uuid.uuid4()
        job = await _job(db_session, seed_tenant, submission_id)
        await db_session.commit()

        await save_checkpoint(
            db_session,
            job.id,
            PathCheckpoint.started(_KEY, ["safety"]),
            current_stage="safety",
        )

        fresh = await JobRepository(db_session).get_by_id(job.id)
        assert fresh is not None
        assert fresh.current_stage == "safety"
        assert fresh.stage_progress is not None
        assert "current_stage" not in fresh.stage_progress


class TestReadingTheLatestJob:
    async def test_reads_the_newest_job_of_the_revision(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """A continuation runs in a new job; the work it continues is in the old.

        The first job is taken out of flight before the second is created,
        because that is the only order the database allows (see
        ``test_two_jobs_of_one_revision_cannot_be_in_flight_at_once``) and the
        one production follows: freezing ends the job normally, and the
        continuation queues a new one.
        """
        submission_id = uuid.uuid4()
        first = await _job(db_session, seed_tenant, submission_id)
        await db_session.commit()
        await save_checkpoint(
            db_session,
            first.id,
            PathCheckpoint.started(_KEY, ["safety", "attempt_classifier"])
            .with_stage_done("safety")
            .frozen("attempt_classifier", FreezeReason.STAGE_MONEY_CEILING),
            current_stage="attempt_classifier",
        )
        await JobRepository(db_session).update_status(first.id, "active")
        await JobRepository(db_session).update_status(first.id, "complete")
        await db_session.commit()

        second = await _job(db_session, seed_tenant, submission_id)
        await db_session.commit()
        await save_checkpoint(
            db_session,
            second.id,
            PathCheckpoint.started(_KEY, ["safety", "attempt_classifier"])
            .with_stage_done("safety")
            .with_stage_done("attempt_classifier"),
            current_stage=None,
        )

        back = await load_checkpoint(db_session, submission_id)
        assert back is not None
        assert back.stages["attempt_classifier"] is StageState.DONE
        assert back.frozen_reason is None

    async def test_two_jobs_of_one_revision_cannot_be_in_flight_at_once(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """The database itself holds the rule the continuation design rests on.

        ``uq_jobs_subject_in_flight`` is a partial unique index on
        ``(subject_type, subject_id)`` where the status is ``queued`` or
        ``active``. So a frozen revision CANNOT keep its job in flight and be
        continued by a new one — the second insert is refused. That is why
        freezing ends the job normally and the continuation queues a fresh one
        (task 03, the ratified third option), and not a matter of taste.
        """
        submission_id = uuid.uuid4()
        await _job(db_session, seed_tenant, submission_id)
        await db_session.commit()

        with pytest.raises(IntegrityError, match="uq_jobs_subject_in_flight"):
            await _job(db_session, seed_tenant, submission_id)
            await db_session.commit()
        await db_session.rollback()

    async def test_another_revisions_job_is_not_read(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        mine = uuid.uuid4()
        theirs = uuid.uuid4()
        their_job = await _job(db_session, seed_tenant, theirs)
        await db_session.commit()
        await save_checkpoint(
            db_session,
            their_job.id,
            PathCheckpoint.started(_KEY, ["safety"]).with_stage_done("safety"),
            current_stage=None,
        )

        assert await load_checkpoint(db_session, mine) is None

    async def test_a_job_without_a_checkpoint_reads_as_none(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """Every submission on today's Mentor is exactly this case."""
        submission_id = uuid.uuid4()
        await _job(db_session, seed_tenant, submission_id)
        await db_session.commit()

        assert await load_checkpoint(db_session, submission_id) is None

    async def test_an_unreadable_record_reads_as_none(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """Start over rather than resume from something nobody can parse."""
        submission_id = uuid.uuid4()
        job = await _job(db_session, seed_tenant, submission_id)
        await JobRepository(db_session).update_stage_progress(
            job.id, {"not": "a checkpoint"}
        )
        await db_session.commit()

        assert await load_checkpoint(db_session, submission_id) is None

    async def test_a_soft_deleted_job_is_not_read(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        from datetime import UTC, datetime

        submission_id = uuid.uuid4()
        job = await _job(db_session, seed_tenant, submission_id)
        await db_session.commit()
        await save_checkpoint(
            db_session,
            job.id,
            PathCheckpoint.started(_KEY, ["safety"]).with_stage_done("safety"),
            current_stage=None,
        )
        job.deleted_at = datetime.now(UTC)
        await db_session.commit()

        assert await load_checkpoint(db_session, submission_id) is None
