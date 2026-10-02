"""Putting a held revision back in the queue (mentor-rebuild task 03).

Purpose:
    A submission on the rebuilt Mentor's path can stop between stages and be
    picked up later. Stopping ENDS the job that was running it — the body
    returns, the execution seam writes its terminal — because the database
    allows a subject only one job in flight at a time
    (``uq_jobs_subject_in_flight``, the idempotency key). So every continuation
    is a NEW job for the same revision, and this module is where one is made.

Interface:
    Several events, one mechanism underneath:

    * :func:`resume_after_top_up` — the account was funded. The entry a billing
      adapter will call; nothing calls it yet, by design.
    * :func:`sweep_frozen_revisions` — the worker started, and a revision is
      held by a limit that an edit of the configuration may have raised, or
      waits for its task's criteria list.
    * :func:`resume_awaiting_criteria` — a criteria list is in force for a
      task: the evaluation stage got one, or the author edited it (task 09b).
    * the retry of a provider's bad minute is NOT here: the body re-queues
      itself while its job is still running (``arq.Retry``), so no new job is
      needed and no one has to find it.

    All of them go through :func:`start_continuation_job`, which refuses to make
    a second job while one is in flight.

Extending:
    A new event is a function that finds its revisions and calls
    :func:`start_continuation_job` on each. What "found" means is the event's
    own business; what happens next is not.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from course_supporter.enqueue import create_homework_job, dispatch_homework
from course_supporter.homework.path_checkpoint import (
    FreezeReason,
    PathCheckpoint,
)
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    get_path_config,
)
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.models.source import AssignmentType
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.job_repository import (
    IN_FLIGHT_STATUSES,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    HomeworkStatus,
    HomeworkSubmission,
    Job,
)

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

_SUBJECT_TYPE = JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING]

CONFIGURATION_FREEZES: frozenset[FreezeReason] = frozenset(
    {FreezeReason.STAGE_MONEY_CEILING, FreezeReason.OUTPUT_CEILING}
)
"""The two holds an edit of ``config/submission_paths.yaml`` can lift.

``AWAITING_FUNDS`` is deliberately NOT one of them: money arrives through the
account, not through a file, and sweeping it at every worker start would put a
revision back in the queue to be refused again. ``PROVIDER_UNAVAILABLE`` is not
one either — that one is retried by the body itself, while its job still runs.
"""


async def start_continuation_job(
    session: AsyncSession,
    arq: ArqRedis,
    *,
    tenant_id: uuid.UUID,
    submission_id: uuid.UUID,
    leave_hold: bool = False,
) -> bool:
    """Make and dispatch a new job for a held revision, if it may have one.

    ``False`` when the revision already has a job in flight: the database would
    refuse the insert (``uq_jobs_subject_in_flight``), and asking first is how
    an expected state stays out of the error log. That is also why a hold ends
    its job rather than keeping it — see the module docstring.

    ``leave_hold`` writes ``received`` in the same transaction as the job, and
    only when the job is made: for a revision that today's Mentor will go on
    with, which starts from ``received`` and knows nothing of holds. A revision
    the new path goes on with keeps its hold until its run starts — a dispatch
    that never reaches the queue then leaves it where the next pass finds it.

    Commits before dispatching, so the worker can only ever read durable rows
    (``DD-3.2.6-A``, the order the first dispatch uses too).
    """
    in_flight = await session.execute(
        select(Job.id)
        .where(
            Job.subject_type == _SUBJECT_TYPE,
            Job.subject_id == submission_id,
            Job.status.in_(IN_FLIGHT_STATUSES),
            Job.deleted_at.is_(None),
        )
        .limit(1)
    )
    if in_flight.scalar_one_or_none() is not None:
        logger.info(
            "path_continuation_skipped_job_in_flight",
            submission_id=str(submission_id),
        )
        return False

    if leave_hold:
        await HomeworkRepository(session).update_status(submission_id, "received")
    job = await create_homework_job(
        session=session, tenant_id=tenant_id, submission_id=submission_id
    )
    await session.commit()
    await dispatch_homework(
        redis=arq, session=session, job_id=job.id, submission_id=submission_id
    )
    logger.info(
        "path_continuation_dispatched",
        submission_id=str(submission_id),
        job_id=str(job.id),
    )
    return True


async def resume_after_top_up(
    session: AsyncSession, arq: ArqRedis, submission_id: uuid.UUID
) -> bool:
    """Continue a revision that was held for funds.

    The entry a billing adapter calls when an account is funded. Nothing in the
    codebase calls it yet, and that is the point: the path is ready for the
    event before the thing that raises it exists, and the seam between them is
    one function rather than a change to the body.

    What lifts the hold is NOT this function: it makes the job, and the body of
    the path takes the revision out of ``awaiting_funds`` once the port has
    allowed it — before a stage runs, because every other status a run writes is
    unreachable from a hold.

    ``False`` when the revision is not held for funds at all, or already has a
    job in flight.
    """
    submission = await HomeworkRepository(session).get_by_id(submission_id)
    if submission is None or submission.status != "awaiting_funds":
        return False
    return await start_continuation_job(
        session,
        arq,
        tenant_id=submission.tenant_id,
        submission_id=submission_id,
    )


async def resume_awaiting_criteria(
    session: AsyncSession,
    arq: ArqRedis,
    *,
    tenant_id: uuid.UUID,
    authored_document_id: uuid.UUID,
    config: PathConfig | None = None,
    first_only: bool = False,
) -> int:
    """Continue the revisions of a task that wait for its criteria list.

    Called where a list has just come into force — by the evaluation stage
    that got one, and by the author's edit of one — and at worker start (task
    09b, ``PRE-FLIGHT.md`` 9.2). Each revision gets a new job through
    :func:`start_continuation_job`, so two entries in a row make one job, not
    two: the second finds the first one's job in flight. Once that run starts,
    it takes the revision out of ``awaiting_criteria`` before anything else,
    and no entry finds it again.

    A task whose type is no longer on the new path is left to today's Mentor,
    which reviews without a list (decided 2026-10-02): the hold is lifted with
    the job, because today's Mentor starts only from ``received``.

    ``config`` is the path configuration; ``None`` reads the one the process
    serves.

    ``first_only`` continues the oldest revision and leaves the rest to it: at
    worker start the list is usually still missing, so the one continuation
    tries to compose it, and if it succeeds its evaluation stage continues the
    others. A composition is not tried once per waiting revision. A task left
    to today's Mentor gets all of them: nothing there continues the rest.

    Returns how many were dispatched.
    """
    document = await session.get(AuthoredDocument, authored_document_id)
    waiting = await HomeworkRepository(session).held_for_criteria(
        tenant_id=tenant_id, authored_document_id=authored_document_id
    )
    if document is None or not waiting:
        return 0
    leave_hold = not _on_the_new_path(
        config if config is not None else get_path_config(), document.task_type
    )
    if first_only and not leave_hold:
        waiting = waiting[:1]
    dispatched = 0
    for submission in waiting:
        if await start_continuation_job(
            session,
            arq,
            tenant_id=tenant_id,
            submission_id=submission.id,
            leave_hold=leave_hold,
        ):
            dispatched += 1
    if dispatched:
        logger.info(
            "awaiting_criteria_continued",
            authored_document_id=str(authored_document_id),
            count=dispatched,
            to_todays_mentor=leave_hold,
        )
    return dispatched


def _on_the_new_path(config: PathConfig, task_type: str | None) -> bool:
    """Is a task of this type served by the new path today?"""
    if task_type not in set(AssignmentType):
        return False
    declared = config.task_types.get(AssignmentType(task_type))
    return declared is not None and declared.served_by is ServedBy.NEW_PATH


async def sweep_frozen_revisions(
    session_factory: async_sessionmaker[AsyncSession],
    arq: ArqRedis,
    *,
    config: PathConfig | None = None,
) -> int:
    """Put back the revisions a configuration edit may have unblocked.

    Runs once at worker startup, beside the orphan sweep and separate from it:
    an orphan is a job nobody is running, this is a revision nobody is running.

    What it looks for: the jobs of homework submissions that recorded a
    checkpoint, newest first per revision, held by one of
    :data:`CONFIGURATION_FREEZES`. Revisions held for funds are left alone, and
    so is every job of today's Mentor — those write no checkpoint at all, which
    is what makes them invisible here without a single special case.

    Then the revisions that wait for their task's criteria list (task 09b):
    one per task (:func:`resume_awaiting_criteria`, ``first_only``), so a
    composition that failed is tried again at most once per start and task
    version. ``config`` is the path configuration; ``None`` reads the one the
    worker serves.

    Returns how many were dispatched. Best-effort by construction: a revision
    that cannot be continued now is simply not, and the next start tries again.
    """
    config = config if config is not None else get_path_config()
    dispatched = 0
    async with session_factory() as session:
        rows = await session.execute(
            select(Job)
            .where(
                Job.subject_type == _SUBJECT_TYPE,
                Job.stage_progress.isnot(None),
                Job.deleted_at.is_(None),
            )
            .order_by(Job.queued_at.desc(), Job.id.desc())
        )
        seen: set[uuid.UUID] = set()
        held: list[Job] = []
        for job in rows.scalars():
            if job.subject_id is None or job.subject_id in seen:
                continue
            seen.add(job.subject_id)  # newest first, so this is the latest one
            checkpoint = _checkpoint_of(job)
            if checkpoint is not None and checkpoint.frozen_reason in (
                CONFIGURATION_FREEZES
            ):
                held.append(job)

        for job in held:
            if job.tenant_id is None or job.subject_id is None:  # pragma: no cover
                continue
            if await start_continuation_job(
                session,
                arq,
                tenant_id=job.tenant_id,
                submission_id=job.subject_id,
            ):
                dispatched += 1

        tasks = await session.execute(
            select(
                HomeworkSubmission.tenant_id, HomeworkSubmission.authored_document_id
            )
            .where(
                HomeworkSubmission.status == HomeworkStatus.AWAITING_CRITERIA.value,
                HomeworkSubmission.deleted_at.is_(None),
            )
            .distinct()
        )
        for tenant_id, authored_document_id in tasks.all():
            dispatched += await resume_awaiting_criteria(
                session,
                arq,
                tenant_id=tenant_id,
                authored_document_id=authored_document_id,
                config=config,
                first_only=True,
            )

    if dispatched:
        logger.info("frozen_revisions_swept", count=dispatched)
    return dispatched


def _checkpoint_of(job: Job) -> PathCheckpoint | None:
    """Parse a job's checkpoint, or ``None`` when it does not carry one."""
    if not isinstance(job.stage_progress, dict):
        return None
    try:
        return PathCheckpoint.from_jsonb(job.stage_progress)
    except ValueError:
        return None


async def resume_orphaned_path_job(
    job: Job, arq: ArqRedis, session: AsyncSession
) -> bool:
    """Re-dispatch an orphaned job that carries a checkpoint, instead of failing it.

    The worker died with this job in flight. Its revision has work behind it and
    a record of where it stopped, so the honest reaction is to run it again from
    there — the job stays in flight, ARQ simply gets a fresh handle for it, and
    the seam treats the re-entry as a replay (``active → active`` is an
    idempotent no-op).

    ``False`` when the job carries no checkpoint, which is every job of today's
    Mentor and every job of another pipeline: those keep the sweep's own
    behaviour, unchanged.
    """
    if job.job_type != JobType.HOMEWORK_PROCESSING.value:
        return False
    if _checkpoint_of(job) is None or job.subject_id is None:
        return False
    await dispatch_homework(
        redis=arq, session=session, job_id=job.id, submission_id=job.subject_id
    )
    logger.info(
        "orphaned_path_job_requeued",
        job_id=str(job.id),
        submission_id=str(job.subject_id),
    )
    return True
