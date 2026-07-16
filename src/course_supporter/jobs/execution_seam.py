"""Execution seam — the single owner of the ``Job.status`` lifecycle (L2).

Vision §3 KD13 / job-lifecycle-contract §L2. One entry and one exit for every
Job-bearing ARQ task, so no background task can "forget to report" (№13, F5),
crash on a terminal write after cancellation (the 2026-07-10 incident class,
F1), or behave differently when its ``Job`` row is missing (DD-3.2.6-B).

Form factor (forced by the environment — ARQ exposes no job-level hooks, only
``on_startup`` / ``on_shutdown``): a per-task **decorator** (:func:`through_seam`,
structurally visible so the completeness test-lock can assert coverage) over a
core routine that owns its **own** DB sessions for the status writes — separate
from the body's sessions, so a terminal write survives a body-side rollback.

Boundary (architectural invariant — contract §L2, Рат.2): the seam knows ONLY
``Job`` (status / started_at / completed_at / result_data / error_*) and the
liveness resolver. ALL domain reaction (the ``HomeworkSubmission`` machine,
webhooks, ``IngestionCallback``'s domain half) lives in the body or in an
**opaque** post-terminal callback the seam invokes without knowing what it does.
Pulling domain logic into the seam is a STOP-escalate (the "super-orchestrator"
anti-pattern, Phase 1.2 carry-forward).

Lifecycle a wrapped task goes through:

* **Entry** — read the ``Job``:
    * missing → single missing-job policy (:func:`_handle_missing_job`): bounded
      ``arq.Retry`` on the transient window, then a loud log + silent exit on the
      final attempt. Not infinite retry, not silent swallow (Рат.3).
    * already at-rest (``complete`` / ``failed`` / ``cancelled`` / ``obsolete``)
      → silent exit. This entry-check is what structurally prevents the
      cancelled→terminal crash class (F1): the body never runs on a job the
      world already terminated.
    * subject soft-deleted (``deleted_at IS NOT NULL``) → ``obsolete`` + silent
      exit (skip-if-dead). ``subject_type IS NULL`` (s3_cleanup, or a legacy
      NULL-subject row — A3) skips the liveness check entirely.
    * otherwise → ``active`` (+ ``started_at``; a no-op on replay).
* **Body** — the wrapped task runs. A normal return → ``complete`` (+
  ``store_result`` when the body returned a dict). An exception → ``failed`` (+
  its structural ``error_category`` when it carries one). **``arq.Retry`` is
  control flow, NOT a failure** (GO condition 1): it propagates untouched — the
  seam neither converts it to ``failed`` nor swallows it, so ARQ's retry and any
  domain-driven retry keep working.
* **Post-terminal callback** (optional, opaque) — invoked AFTER the terminal
  ``Job`` write commits, so "terminal-first" ordering is preserved for consumers
  that need it (ingest: the failed-Job write must commit before the dependent
  cascade, or a stranded ``active`` blocks the single serial worker's queue).
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from arq import Retry

from course_supporter.config import get_settings
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.storage.job_repository import (
    AT_REST_STATUSES,
    JobRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

# Bounded backoff base for the missing-job policy (seconds). ARQ caps the number
# of attempts via ``max_tries`` (derived from Settings, A2); this only shapes the
# defer between them so a genuinely out-of-band enqueue does not hot-loop.
_MISSING_JOB_DEFER_BASE_S = 2

# ── Liveness resolver ────────────────────────────────────────────────────
# The subject_type tokens the seam can check for liveness — EXACTLY the non-NULL
# values of ``JOB_SUBJECT_TYPE`` (the L1b single source). Liveness is ONE uniform
# predicate for all of them (``deleted_at IS NULL``, every subject is a
# ``SoftDeleteMixin``); only the FETCH differs per type. So this is a set of
# mechanical fetchers under one rule, NOT a second classifier (урок №18).
_LIVENESS_SUBJECT_TYPES: frozenset[str] = frozenset(
    {"authored_document", "homework_submission", "course_node", "project_base"}
)

# Derive-or-verify guard (mirror of the L1a ``_STATUS_CATEGORY`` and L1b
# ``JOB_SUBJECT_TYPE`` totality guards): a new subject type added to
# ``JOB_SUBJECT_TYPE`` without a liveness fetcher here fails LOUDLY at import —
# not silently as an unresolved subject the seam would wrongly treat as alive.
_expected_liveness = {st for st in JOB_SUBJECT_TYPE.values() if st is not None}
if _expected_liveness != _LIVENESS_SUBJECT_TYPES:  # pragma: no cover — test-locked
    msg = (
        "execution_seam liveness resolver out of step with JOB_SUBJECT_TYPE: "
        f"resolver={sorted(_LIVENESS_SUBJECT_TYPES)} "
        f"expected={sorted(_expected_liveness)}. Add a liveness fetcher for the "
        "new subject type (or remove the stale one)."
    )
    raise RuntimeError(msg)


async def _fetch_subject(
    session: AsyncSession, subject_type: str, subject_id: uuid.UUID
) -> Any:
    """Fetch the subject entity by type (repos imported lazily to avoid the
    jobs↔storage import cycle). Returns the entity or ``None`` if absent.

    The membership of the branches here is guard-locked to
    ``_LIVENESS_SUBJECT_TYPES`` above; an unknown token is a programming error
    (the enqueue write side and the DB CHECK both constrain subject_type to the
    legal set), so it raises rather than silently treating the subject as alive.
    """
    if subject_type == "authored_document":
        from course_supporter.storage.authored_document_repository import (
            AuthoredDocumentRepository,
        )

        return await AuthoredDocumentRepository(session).get_by_id(subject_id)
    if subject_type == "homework_submission":
        from course_supporter.storage.homework_repository import HomeworkRepository

        return await HomeworkRepository(session).get_by_id(subject_id)
    if subject_type == "course_node":
        from course_supporter.storage.course_node_repository import (
            CourseNodeRepository,
        )

        return await CourseNodeRepository(session).get_by_id(subject_id)
    if subject_type == "project_base":
        from course_supporter.storage.project_base_repository import (
            ProjectBaseRepository,
        )

        return await ProjectBaseRepository(session).get_by_id(subject_id)
    msg = f"No liveness fetcher for subject_type {subject_type!r}"
    raise ValueError(msg)


async def _subject_is_alive(
    session: AsyncSession, subject_type: str, subject_id: uuid.UUID
) -> bool:
    """One uniform liveness predicate: the subject exists and is not
    soft-deleted. A missing subject counts as not-alive (skip-if-dead)."""
    entity = await _fetch_subject(session, subject_type, subject_id)
    return entity is not None and getattr(entity, "deleted_at", None) is None


# ── Terminal outcome + post-terminal callback ────────────────────────────


@dataclass(frozen=True)
class SeamTerminal:
    """The terminal the seam wrote, handed to an opaque post-terminal callback.

    ``result`` is the body's return value (persisted via ``store_result`` when a
    dict); ``error_*`` are populated only on the ``failed`` path.
    """

    status: str  # "complete" | "failed" | "obsolete"
    error_message: str | None = None
    error_category: ErrorCategory | None = None
    result: dict[str, Any] | None = None


# A task body: ``(ctx, job_id, *args) -> dict | None``. A dict return is stored
# on ``Job.result_data``. The seam owns status; the body owns domain.
TaskBody = Callable[..., Awaitable[dict[str, Any] | None]]
# An opaque post-terminal hook: invoked with the SAME task args + the outcome,
# AFTER the terminal Job write commits. The seam does not inspect what it does.
OnTerminal = Callable[..., Awaitable[None]]


def through_seam(
    *, on_terminal: OnTerminal | None = None
) -> Callable[[TaskBody], TaskBody]:
    """Wrap a Job-bearing ARQ task so the seam owns its ``Job.status`` lifecycle.

    Usage::

        @through_seam()
        async def arq_regenerate_node_summary(ctx, job_id, vertex_node_id, ...):
            ...  # pure body: no update_status; return a dict to store_result

    ``on_terminal`` (opaque) runs after the terminal Job write commits, with the
    original task args + a :class:`SeamTerminal` — for consumers that need
    terminal-first ordering (ingest). The decorator marks the function
    (``__seam_wrapped__``) so the completeness test-lock can assert coverage.
    """

    def decorate(body: TaskBody) -> TaskBody:
        @functools.wraps(body)
        async def wrapper(
            ctx: dict[str, Any], job_id: str, *args: Any, **kwargs: Any
        ) -> None:
            await _run_seam(ctx, job_id, body, on_terminal, args, kwargs)

        wrapper.__seam_wrapped__ = True  # type: ignore[attr-defined]
        return wrapper

    return decorate


def _handle_missing_job(ctx: dict[str, Any], job_id: str, logger: Any) -> None:
    """Single missing-job policy (Рат.3, closes DD-3.2.6-B).

    Bounded ``arq.Retry`` on the transient window (out-of-band enqueue, or a Job
    that has not yet become visible), then a loud ERROR + silent exit on the
    final attempt. ``max_tries`` is derived from Settings (A2), ``job_try`` from
    the ARQ ctx; the bound is therefore not hard-coded and stays in step with
    the worker config. Raises ``arq.Retry`` to defer, or returns to give up.
    """
    job_try = int(ctx.get("job_try", 1))
    max_tries = get_settings().worker_max_tries
    if job_try < max_tries:
        logger.warning("seam_job_missing_retry", job_try=job_try, max_tries=max_tries)
        raise Retry(defer=_MISSING_JOB_DEFER_BASE_S * job_try)
    logger.error("seam_job_missing_giving_up", job_try=job_try, max_tries=max_tries)


async def _run_seam(
    ctx: dict[str, Any],
    job_id: str,
    body: TaskBody,
    on_terminal: OnTerminal | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """Drive one task through the seam. See module docstring for the lifecycle."""
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    jid = uuid.UUID(job_id)
    logger = log.bind(job_id=job_id, task=getattr(body, "__name__", "?"))

    # ── Entry ──
    async with session_factory() as session:
        repo = JobRepository(session)
        job = await repo.get_by_id(jid)
        if job is None:
            _handle_missing_job(ctx, job_id, logger)  # raises Retry or returns
            return
        if job.status in AT_REST_STATUSES:
            # complete / failed / cancelled / obsolete — replay or externally
            # terminated. Body must not run (this is the F1 crash-class fix).
            logger.info("seam_skip_terminal", status=job.status)
            return
        # NULL subject (s3_cleanup, or a legacy NULL-subject row — A3) skips
        # liveness entirely. Locals narrow the Optional for the typed call.
        st, sid = job.subject_type, job.subject_id
        if (
            st is not None
            and sid is not None
            and not await _subject_is_alive(session, st, sid)
        ):
            await repo.update_status(jid, "obsolete")
            await session.commit()
            logger.info("seam_obsolete_dead_subject", subject_type=st)
            await _invoke_on_terminal(
                on_terminal, ctx, job_id, args, kwargs, SeamTerminal(status="obsolete")
            )
            return
        await repo.update_status(jid, "active")  # no-op on replay (active→active)
        await session.commit()

    # ── Body ──
    try:
        result = await body(ctx, job_id, *args, **kwargs)
    except Retry:
        # Control flow, NOT a failure (GO condition 1): let ARQ (or the
        # missing-job policy / a domain retry) handle it. Do not terminalize.
        raise
    except Exception as exc:
        category = getattr(exc, "category", None)
        terminal = SeamTerminal(
            status="failed",
            error_message=str(exc),
            error_category=category if isinstance(category, ErrorCategory) else None,
        )
        landed = await _write_terminal(session_factory, jid, terminal, logger)
        # "No terminal — no post-terminal": if the write was skipped (a race
        # already moved the Job to cancelled/terminal), the callback must NOT
        # fire — otherwise the domain would react (cascade, webhook) to a
        # terminal the DB does not carry.
        if landed:
            await _invoke_on_terminal(on_terminal, ctx, job_id, args, kwargs, terminal)
        # Swallow: the Job is terminally `failed` and the domain reaction has run
        # (in the body before it re-raised, or in on_terminal). Re-raising would
        # make ARQ retry an already-terminal job.
        return

    # ── Success terminal ──
    terminal = SeamTerminal(status="complete", result=result)
    landed = await _write_terminal(session_factory, jid, terminal, logger)
    if landed:
        await _invoke_on_terminal(on_terminal, ctx, job_id, args, kwargs, terminal)


async def _write_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    jid: uuid.UUID,
    terminal: SeamTerminal,
    logger: Any,
) -> bool:
    """Write the terminal status (+ result_data) in the seam's OWN fresh session.

    Fresh (not the body's, which may be poisoned by a rollback) so the terminal
    survives independently. Returns ``True`` when the terminal landed, ``False``
    when it was skipped. A ``ValueError`` here means the transition is illegal
    (e.g. a race already moved the Job to a terminal / cancelled state) — nothing
    is stranded, so log and report the skip so the caller withholds the opaque
    post-terminal callback ("no terminal — no post-terminal").
    """
    async with session_factory() as session:
        repo = JobRepository(session)
        try:
            await repo.update_status(
                jid,
                terminal.status,
                error_message=terminal.error_message,
                error_category=terminal.error_category,
            )
            if terminal.result is not None:
                await repo.store_result(jid, terminal.result)
            await session.commit()
        except ValueError as exc:
            await session.rollback()
            logger.warning(
                "seam_terminal_write_skipped",
                target=terminal.status,
                reason=str(exc),
            )
            return False
        return True


async def _invoke_on_terminal(
    on_terminal: OnTerminal | None,
    ctx: dict[str, Any],
    job_id: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    terminal: SeamTerminal,
) -> None:
    """Invoke the opaque post-terminal callback (if any) with the task args +
    outcome, AFTER the terminal write. Best-effort: a callback failure must not
    undo the durable terminal write."""
    if on_terminal is None:
        return
    try:
        await on_terminal(ctx, job_id, *args, outcome=terminal, **kwargs)
    except Exception:
        log.exception("seam_on_terminal_failed", job_id=job_id)
