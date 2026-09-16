"""The body of the rebuilt Mentor's path: doors, funds, stages, finish (task 03).

Purpose:
    One linear body. It opens the same free doors today's Mentor opens, asks the
    funds port once before anything is paid for, walks the stage list its path
    resolved to, and finishes the revision. It knows the NAMES of its stages and
    not their meanings: a stage is a description plus an executor, and what a
    stage decided comes back as a value the body acts on
    (:class:`~course_supporter.homework.path_stages.StageOutcome`).

Interface:
    :func:`run_new_path_if_switched` is what the ARQ task calls as its first
    step. It answers ``False`` — having touched nothing — when the submission's
    type is served by today's Mentor, which is every type in production after
    this task; the caller then runs today's body unchanged. It answers ``True``
    when it has handled the submission itself.

    The funds port is a parameter, so a billing adapter replaces it without a
    line changing here (:class:`~course_supporter.funds_port.FundsPort`).

Extending:
    A new stage is a definition in ``config/submission_paths.yaml`` plus an
    executor in :mod:`course_supporter.homework.path_stages`; this module does
    not change. Freezing and continuation are task 03's next unit and live
    beside this one.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog

from course_supporter.funds_port import (
    AlwaysEnoughFundsPort,
    SubmissionContext,
    SubmissionOutcome,
)
from course_supporter.homework.path_checkpoint import (
    FREEZE_REASON_FOR_LADDER_STOP,
    PathCheckpoint,
    load_checkpoint,
    save_checkpoint,
)
from course_supporter.homework.path_config import (
    ServedBy,
    get_path_config,
    path_ceiling_estimate,
)
from course_supporter.homework.path_selection import choose_path
from course_supporter.homework.path_stages import (
    StageContext,
    StageOutcome,
    get_stage_executor,
)
from course_supporter.llm.error_categories import LadderExhaustedError, LadderStop
from course_supporter.models.source import AssignmentType
from course_supporter.storage.homework_repository import HomeworkRepository

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.funds_port import FundsPort
    from course_supporter.homework.path_config import PathConfig
    from course_supporter.homework.path_selection import PathChoice
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.storage.orm import HomeworkSubmission, Student, Tenant
    from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger(__name__)


def _nothing_is_switched(config: PathConfig) -> bool:
    """Is every task type still served by today's Mentor?

    The cheapest possible question, asked first: in production after this task
    the answer is yes for all four types, and the homework body then costs
    exactly what it cost before — one scan of a dict already in memory, no
    database, no file.
    """
    return all(
        declared.served_by is not ServedBy.NEW_PATH
        for declared in config.task_types.values()
    )


async def run_new_path_if_switched(
    ctx: dict[str, Any],
    job_id: uuid.UUID,
    submission_id: uuid.UUID,
    *,
    funds_port: FundsPort | None = None,
) -> bool:
    """Run the new path for this submission, or answer ``False`` for today's.

    ``False`` means nothing was read, nothing was written, and the caller must
    run today's body — byte for byte what it did before this task existed.
    """
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    config = get_path_config()
    if _nothing_is_switched(config):
        return False

    async with session_factory() as session:
        submission = await HomeworkRepository(session).get_by_id(submission_id)
        if submission is None:
            return False
        # A revision that has already walked part of a path continues on the
        # one it was given, never on one chosen again: the state it was chosen
        # from may have changed since (its own first review, for one), and a
        # continuation that re-chose could hand the student a different path
        # halfway through their submission.
        resumed = await load_checkpoint(session, submission_id)
        if resumed is not None:
            choice = _choice_from(resumed, config)
        else:
            task_doc = await _task_document(session, submission)
            if task_doc is None:
                return False
            choice = await choose_path(
                session,
                task_type=AssignmentType(task_doc.task_type),
                student_id=submission.student_id,
                authored_document_id=submission.authored_document_id,
                config=config,
            )
        if choice is None:
            return False

    port = (
        funds_port if funds_port is not None else AlwaysEnoughFundsPort(session_factory)
    )
    async with session_factory() as session:
        await _run_path(
            ctx,
            session,
            job_id=job_id,
            submission_id=submission_id,
            choice=choice,
            config=config,
            port=port,
            resumed=resumed,
        )
    return True


def _choice_from(checkpoint: PathCheckpoint, config: PathConfig) -> PathChoice | None:
    """The path a checkpoint says this revision is on, as it is described today.

    The KEY comes from the record; the STAGE LIST comes from the configuration
    as it stands now, because lifting a freeze is exactly an edit of that file.
    :meth:`PathCheckpoint.first_unfinished` reconciles the two.

    ``None`` when the type has since been switched back to today's Mentor, or
    its paths were removed: a revision cannot be continued down a path that no
    longer exists, and the caller then leaves it to today's body.
    """
    from course_supporter.homework.path_selection import PathChoice

    declared = config.task_types.get(checkpoint.task_type)
    if declared is None or checkpoint.submission_state not in declared.paths:
        return None
    return PathChoice(
        key=checkpoint.path_key,
        stages=tuple(declared.paths[checkpoint.submission_state]),
    )


async def _task_document(session: AsyncSession, submission: HomeworkSubmission) -> Any:
    from course_supporter.storage.authored_document_repository import (
        AuthoredDocumentRepository,
    )

    return await AuthoredDocumentRepository(session).get_by_id(
        submission.authored_document_id
    )


async def _run_path(
    ctx: dict[str, Any],
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    submission_id: uuid.UUID,
    choice: PathChoice,
    config: PathConfig,
    port: FundsPort,
    resumed: PathCheckpoint | None = None,
) -> None:
    """The linear body, in the order of the skeleton (03-BINDING decision 1)."""
    router: StageRouter = ctx["stage_router"]
    s3: S3Client = ctx["s3_client"]
    hw_repo = HomeworkRepository(session)
    log = logger.bind(
        job_id=str(job_id),
        submission_id=str(submission_id),
        path_key=str(choice.key),
    )

    submission = await hw_repo.get_by_id(submission_id)
    if submission is None:
        msg = f"HomeworkSubmission {submission_id} not found"
        raise ValueError(msg)
    student, tenant = await _student_and_tenant(session, submission)

    # ── Doors: free, synchronous, and the same functions today's Mentor uses ──
    file_path, submission_text, language = await _open_the_doors(
        session, s3, hw_repo, submission, job_id=job_id, log=log
    )
    if submission_text is None:
        # The doors refused and persisted their own refusal, as they do today.
        return
    try:
        # ── The first funds question, before anything is paid for ──
        context = SubmissionContext(
            tenant_id=submission.tenant_id,
            student_id=submission.student_id,
            submission_id=submission.id,
            path_key=choice.key,
        )
        answer = await port.check_and_reserve(
            context, path_ceiling_estimate(config, choice.key)
        )
        checkpoint = (
            resumed
            if resumed is not None
            else PathCheckpoint.started(choice.key, choice.stages)
        )
        if answer.decision.value == "refused":
            await _hold_for_funds(
                session, hw_repo, submission_id, job_id, checkpoint, answer, log=log
            )
            return
        if submission.status == "awaiting_funds":
            # A hold is a state the revision has to LEAVE before it can reach
            # any other. `awaiting_funds` leads only to `received` or `failed`,
            # so a continuation that walked straight on would run every stage,
            # pay for every stage, and then be refused its own result at the
            # finish — the submission stranded on the hold for good, because the
            # startup pass deliberately never touches one. The edge back to
            # `received` exists for exactly this moment: take it first, before
            # anything is written or spent.
            await hw_repo.update_status(submission_id, "received")
            await session.commit()
        await save_checkpoint(session, job_id, checkpoint, current_stage=None)

        # ── The stages of the path, one after another ──
        start_from = checkpoint.first_unfinished(list(choice.stages))
        for stage_name in choice.stages:
            if start_from is not None and stage_name != start_from:
                continue
            start_from = None
            try:
                outcome = await _run_stage(
                    session,
                    router,
                    submission=submission,
                    submission_text=submission_text,
                    language=language,
                    choice=choice,
                    config=config,
                    stage_name=stage_name,
                )
            except LadderExhaustedError as exhausted:
                await _stage_produced_nothing(
                    session,
                    hw_repo,
                    port,
                    context,
                    submission_id=submission_id,
                    job_id=job_id,
                    checkpoint=checkpoint,
                    stage_name=stage_name,
                    stop=exhausted.stop,
                    job_try=int(ctx.get("job_try", 1)),
                    log=log,
                )
                return
            checkpoint = checkpoint.with_stage_done(stage_name)
            await save_checkpoint(session, job_id, checkpoint, current_stage=stage_name)
            await port.account_stage_cost(
                context, await _stage_cost(session, job_id, stage_name)
            )
            if not outcome.carry_on:
                await _end_path_early(session, hw_repo, submission_id, outcome, log=log)
                await port.release_remainder(context, SubmissionOutcome.COMPLETED)
                return

        # ── Finish ──
        await _finish(
            session,
            hw_repo,
            submission=submission,
            student=student,
            tenant=tenant,
            log=log,
        )
        await port.release_remainder(context, SubmissionOutcome.COMPLETED)
    finally:
        if file_path is not None and file_path.exists():
            file_path.unlink()


async def _student_and_tenant(
    session: AsyncSession, submission: HomeworkSubmission
) -> tuple[Student | None, Tenant | None]:
    from course_supporter.storage.orm import Tenant
    from course_supporter.storage.student_repository import StudentRepository

    student = await StudentRepository(session).get_by_id(submission.student_id)
    tenant = await session.get(Tenant, submission.tenant_id)
    return student, tenant


async def _open_the_doors(
    session: AsyncSession,
    s3: S3Client,
    hw_repo: HomeworkRepository,
    submission: HomeworkSubmission,
    *,
    job_id: uuid.UUID,
    log: Any,
) -> tuple[Path | None, str | None, str | None]:
    """Read what can be read, for free, and refuse what cannot be.

    The same three functions today's Mentor calls — the archive/single-file
    Stage 1, the text budget, the project normalizer — and the same two shapes
    of refusal. Deliberately NOT shared with today's body: sharing would mean
    editing it, and it is not to be touched while it serves production
    (``DD-SP-AM`` records the same choice for the ladders).

    Returns ``(temp file, text, language)``; a ``None`` text means the doors
    refused and already wrote why.
    """
    from course_supporter.homework.doors import (
        assemble_submission_text,
        extract_document_text,
    )
    from course_supporter.homework.project_submission import (
        process_project_submission,
    )
    from course_supporter.language import resolve_review_language
    from course_supporter.normalizer.classify import denylist_prefix
    from course_supporter.security.exceptions import SecurityRejectedError
    from course_supporter.security.schemas import Stage1RejectionResult
    from course_supporter.security.stage1 import run_stage1
    from course_supporter.storage.authored_document_repository import (
        AuthoredDocumentRepository,
    )
    from course_supporter.storage.student_repository import StudentRepository

    s3_key = s3.extract_key(submission.file_url)
    if s3_key is None:
        msg = f"Cannot extract S3 key from {submission.file_url}"
        raise ValueError(msg)
    file_path = await s3.download_file(s3_key)

    student = await StudentRepository(session).get_by_id(submission.student_id)
    task_doc = await AuthoredDocumentRepository(session).get_by_id(
        submission.authored_document_id
    )
    review_language = resolve_review_language(
        explicit=submission.response_language,
        preferred=student.preferred_language if student else None,
        course=task_doc.language if task_doc is not None else None,
    )
    course_language = task_doc.language if task_doc is not None else None
    verify_languages = [
        code for code in dict.fromkeys([course_language, review_language.code]) if code
    ]

    file_bytes = file_path.read_bytes()
    if task_doc is not None and task_doc.task_type == AssignmentType.PROJECT.value:
        project_text = await process_project_submission(
            session=session,
            s3=s3,
            hw_repo=hw_repo,
            submission=submission,
            sid=submission.id,
            jid=job_id,
            file_bytes=file_bytes,
            raw_key=s3_key,
        )
        return file_path, project_text, review_language.code

    try:
        stage1_result = run_stage1(
            filename=submission.original_filename or file_path.name,
            content=file_bytes,
            context="homework",
            languages=verify_languages,
            archive_skip_matcher=denylist_prefix,
            document_extractor=extract_document_text,
        )
        text, _not_opened = assemble_submission_text(
            stage1_result,
            file_bytes=file_bytes,
            filename=(submission.original_filename or file_path.name),
        )
    except SecurityRejectedError as exc:
        rejection = Stage1RejectionResult(category=exc.category, detail=exc.detail)
        await hw_repo.store_safety_result(
            submission.id, rejection.model_dump(mode="json")
        )
        await hw_repo.update_status(submission.id, "rejected", error_message=exc.detail)
        await session.commit()
        log.warning("path_door_refused", category=exc.category.value)
        return file_path, None, review_language.code
    return file_path, text, review_language.code


async def _run_stage(
    session: AsyncSession,
    router: StageRouter,
    *,
    submission: HomeworkSubmission,
    submission_text: str,
    language: str | None,
    choice: PathChoice,
    config: PathConfig,
    stage_name: str,
) -> StageOutcome:
    """Resolve the name to an executor and hand it the stage's description."""
    executor = get_stage_executor(stage_name)
    return await executor(
        StageContext(
            session=session,
            router=router,
            submission=submission,
            submission_text=submission_text,
            language=language,
            path_key=choice.key,
            stage_name=stage_name,
            stage=config.stages[stage_name],
        )
    )


async def _stage_cost(
    session: AsyncSession, job_id: uuid.UUID, stage_name: str
) -> float:
    """What this job actually paid for this stage, from the call register.

    Rows without a call carry a NULL cost and are skipped by the sum, so what
    comes back is the price of the calls that were made (KD5). The sum can be
    short without saying so when a register write was skipped — ``DD-SP-AN``,
    whose visible trace this task adds.
    """
    from sqlalchemy import func, select

    from course_supporter.storage.orm import ExternalServiceCall

    stmt = select(func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0)).where(
        ExternalServiceCall.job_id == job_id,
        ExternalServiceCall.action == stage_name,
    )
    result = await session.execute(stmt)
    total = result.scalar_one()
    # coalesce guarantees a number, but the column is nullable and the
    # type checker reads the column, not the SQL around it.
    return float(total or 0.0)


async def _stage_produced_nothing(
    session: AsyncSession,
    hw_repo: HomeworkRepository,
    port: FundsPort,
    context: SubmissionContext,
    *,
    submission_id: uuid.UUID,
    job_id: uuid.UUID,
    checkpoint: PathCheckpoint,
    stage_name: str,
    stop: LadderStop,
    job_try: int,
    log: Any,
) -> None:
    """A stage ended without a result. What that means depends on WHY.

    Three endings, three reactions, and the difference is read from the
    ladder's own answer rather than from its message (:class:`LadderStop`):

    * the two ceilings — output and money — are held, not retried: the next
      attempt would meet the same limit with the same input, and what has to
      change is the configuration. The revision stays in ``received``, which the
      student reads as "being checked", and the run's checkpoint says where and
      why. The funds port is NOT told: the submission has not ended.
    * an ordinary exhaustion may pass, so the body re-queues itself — up to its
      own limit, counted in the checkpoint — by raising ``arq.Retry``, which the
      execution seam deliberately lets through as control flow rather than as a
      failure.
    * a retry budget spent is the end: ``failed`` with the reason code, which
      the surface reads as "not opened — send it again", and the port hears that
      the submission ended in failure.

    TWO budgets have to be spent for that last one, and the body watches both.
    Its own, counted in the checkpoint, is the one it chose. The queue's
    (``worker_max_tries``) is the one it lives in, and the body must never be
    the reason it runs out: on the FINAL queue attempt the seam turns a re-queue
    into a terminal ``failed`` on the JOB and re-raises — and a revision left
    ``received`` behind a failed job is reachable by none of the three
    continuations (the orphan sweep sees only jobs in flight; the frozen-revision
    pass sees only the two ceilings). So the body ends the submission ITSELF
    while it still can.

    Checking ``job_try`` rather than only comparing the two settings at startup
    is deliberate: the queue's attempts are also spent by things the path does
    not control — the seam's own missing-job policy, for one — so a limit that
    looks safe in the configuration can still be the last attempt in practice.
    """
    from arq import Retry

    from course_supporter.config import get_settings

    reason = FREEZE_REASON_FOR_LADDER_STOP[stop]
    held = checkpoint.frozen(stage_name, reason)

    if stop is not LadderStop.EXHAUSTED:
        await save_checkpoint(session, job_id, held, current_stage=stage_name)
        log.info("path_frozen", stage=stage_name, reason=reason.value)
        return

    settings = get_settings()
    last_queue_attempt = job_try >= settings.worker_max_tries
    if checkpoint.retries < settings.submission_path_max_retries and not (
        last_queue_attempt
    ):
        await save_checkpoint(session, job_id, held.retried(), current_stage=stage_name)
        log.info(
            "path_retrying",
            stage=stage_name,
            retries=held.retries + 1,
            limit=settings.submission_path_max_retries,
        )
        raise Retry(defer=settings.submission_path_retry_defer_s)

    await save_checkpoint(session, job_id, held, current_stage=stage_name)
    await hw_repo.update_status(submission_id, "failed", error_message=reason.value)
    await session.commit()
    await port.release_remainder(context, SubmissionOutcome.FAILED)
    log.info(
        "path_gave_up",
        stage=stage_name,
        retries=checkpoint.retries,
        # Which budget ran out — the path's own, or the queue it lives in.
        last_queue_attempt=last_queue_attempt,
    )


async def _hold_for_funds(
    session: AsyncSession,
    hw_repo: HomeworkRepository,
    submission_id: uuid.UUID,
    job_id: uuid.UUID,
    checkpoint: PathCheckpoint,
    answer: Any,
    *,
    log: Any,
) -> None:
    """The port refused: hold the revision, having spent nothing.

    The hold is lifted by the continuation, not from here: a new job asks the
    port again and the body writes ``received`` before it runs anything.
    """
    from course_supporter.homework.path_checkpoint import FreezeReason

    held = checkpoint.frozen(
        checkpoint.first_unfinished(list(checkpoint.stages)) or "",
        FreezeReason.AWAITING_FUNDS,
    )
    await save_checkpoint(session, job_id, held, current_stage=None)
    await hw_repo.update_status(submission_id, "awaiting_funds")
    await session.commit()
    log.info(
        "path_held_for_funds",
        reason=answer.refusal_reason.value if answer.refusal_reason else None,
    )


async def _end_path_early(
    session: AsyncSession,
    hw_repo: HomeworkRepository,
    submission_id: uuid.UUID,
    outcome: StageOutcome,
    *,
    log: Any,
) -> None:
    """A stage reached an answer that finishes the submission."""
    if outcome.terminal_status is None:  # pragma: no cover — guarded by the type
        msg = "A stage that ends the path must name the status it ends it in"
        raise ValueError(msg)
    await hw_repo.update_status(
        submission_id, outcome.terminal_status, error_message=outcome.reason_code
    )
    await session.commit()
    log.info(
        "path_ended_by_stage",
        status=outcome.terminal_status,
        reason_code=outcome.reason_code,
    )


async def _finish(
    session: AsyncSession,
    hw_repo: HomeworkRepository,
    *,
    submission: HomeworkSubmission,
    student: Student | None,
    tenant: Tenant | None,
    log: Any,
) -> None:
    """Write the revision's result and deliver it, with today's own code.

    With no review stage on the path there is no review to write, so
    ``review_result`` / ``review_markdown`` / ``score`` stay empty and the
    webhook builds from its own defaults — a temporary result until the stages
    that judge arrive (task 07). The delivery half is deliberately the same
    calls today's body makes: the external contract is not this task's to move.
    """
    from course_supporter.homework.webhook import (
        build_reviewed_payload,
        deliver_webhook,
        resolve_webhook_url,
    )

    await hw_repo.update_status(submission.id, "reviewing")
    await session.commit()
    await hw_repo.update_status(submission.id, "completed")
    await session.commit()

    webhook_url = resolve_webhook_url(submission, tenant)
    if webhook_url and student:
        await session.refresh(submission)
        delivered = await deliver_webhook(
            url=webhook_url,
            payload=build_reviewed_payload(submission, student),
            session=session,
        )
        if delivered:
            await hw_repo.update_status(submission.id, "delivered")
    await session.commit()
    log.info("path_finished")
