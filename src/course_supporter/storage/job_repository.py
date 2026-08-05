"""Repository for Job CRUD and status management."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    case,
    distinct,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from course_supporter.jobs import JOB_SUBJECT_TYPE, JobType, validate_job_type
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.storage.orm import AuthoredDocument, CourseNode, Job, ProjectBase

logger = structlog.get_logger()

# Valid job status transitions. Single source of truth for the state
# machine (contract §3 "Ownership"): every status write goes through
# ``update_status``, which consults this table.
#
# ``active → cancelled`` is legal: the soft-delete cascade terminalises
# in-flight jobs (queued OR active) via the cancel sweep, and routing
# that through ``update_status`` requires the transition the machine
# would otherwise reject.
JOB_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"active", "cancelled", "failed", "obsolete"},
    "active": {"complete", "failed", "cancelled", "obsolete"},
    "complete": set(),
    "failed": {"queued"},  # retry
    "cancelled": set(),
    # L2 seam terminal "subject vanished": reachable from either in-flight
    # state (the seam writes it on skip-if-dead, before or after `active`).
    # Terminal — no outgoing edges.
    "obsolete": set(),
}

# Category per status — the ONE source from which the in-flight / at-rest
# partitions are derived (contract Acceptance #3). Hand-maintained partition
# copies drift silently: the dead ``running`` token survived for years inside
# a duplicated tuple. The totality guard below fails loudly if a status is
# added to ``JOB_TRANSITIONS`` without a category here — a new terminal (e.g.
# L2's "subject vanished") cannot be forgotten the way ``running`` was.
#
# NOT derived from graph edges: ``failed → queued`` (retry) gives ``failed``
# an outgoing edge, so "non-terminal = has outgoing edges" would wrongly file
# ``failed`` as in-flight though no worker holds it (probe P3, ratified).
_STATUS_CATEGORY: dict[str, str] = {
    "queued": "in_flight",
    "active": "in_flight",
    "complete": "at_rest",
    "failed": "at_rest",
    "cancelled": "at_rest",
    "obsolete": "at_rest",  # L2 terminal — no worker holds it
}

_uncategorised = set(JOB_TRANSITIONS) - set(_STATUS_CATEGORY)
if _uncategorised:  # pragma: no cover — guarded by test_l1a_invariants
    msg = (
        f"JOB_TRANSITIONS statuses without a _STATUS_CATEGORY entry: "
        f"{sorted(_uncategorised)}. Add each to _STATUS_CATEGORY "
        f"('in_flight' or 'at_rest')."
    )
    raise RuntimeError(msg)

IN_FLIGHT_STATUSES: frozenset[str] = frozenset(
    s for s, cat in _STATUS_CATEGORY.items() if cat == "in_flight"
)
AT_REST_STATUSES: frozenset[str] = frozenset(
    s for s, cat in _STATUS_CATEGORY.items() if cat == "at_rest"
)

# ── Author work-list (step A §4) ──────────────────────────────────────────
# The doors return only author-material work. homework_processing (student
# contour) and s3_cleanup (no material subject) are NEVER author-facing; a
# request outside this set is a 422 at the route (validated there; filtered
# here defensively).
AUTHOR_JOB_TYPES: frozenset[str] = frozenset(
    {
        JobType.DOCUMENT_PROCESSING.value,
        JobType.DOCUMENT_PREPARATION.value,
        JobType.NODE_SUMMARY_REGENERATION.value,
        JobType.BASE_NORMALIZE.value,
    }
)
# History (door 2) further drops node_summary: history is a history of
# MATERIALS (decision P6), and a node-summary job has no material anchor.
AUTHOR_HISTORY_JOB_TYPES: frozenset[str] = frozenset(
    {
        JobType.DOCUMENT_PROCESSING.value,
        JobType.DOCUMENT_PREPARATION.value,
        JobType.BASE_NORMALIZE.value,
    }
)

StateClass = Literal["in_flight", "at_rest"]


@dataclass(frozen=True, slots=True)
class AuthorJobRow:
    """One flat-list row (door 1): a Job plus its resolved material anchor and
    label. Display fields come from LEFT JOINs with NO soft-delete filter, so a
    deleted source contributes its scrubbed marker + ``deleted_at`` (decision
    P7), never a NULL that hides it. ``base_version`` is set only for
    base-normalize work.
    """

    job: Job
    material_id: uuid.UUID | None
    display_name: str | None
    material_deleted_at: datetime | None
    material_source_type: str | None
    base_version: int | None


@dataclass(frozen=True, slots=True)
class MaterialHistoryRow:
    """One grouped row (door 2): a material anchor with its work count, latest
    work, and material-level display data. ``processing_phase`` is the material's
    real phase, ``None`` when the material is deleted (a deleted material is a
    marker, not a phase — decision P7).
    """

    material_id: uuid.UUID
    jobs_count: int
    last_job: AuthorJobRow
    display_name: str | None
    material_source_type: str | None
    material_deleted_at: datetime | None
    processing_phase: str | None


@dataclass(frozen=True, slots=True)
class _AuthorJobQuery:
    """The joined display SELECT plus the material-anchor expression, so callers
    both project the row and filter by anchor without re-declaring the four LEFT
    JOINs (shared by door 1's list/count and door 2's last-work load).
    """

    select: Select[
        tuple[
            Job, uuid.UUID | None, str | None, datetime | None, str | None, int | None
        ]
    ]
    material_anchor: ColumnElement[uuid.UUID | None]


def _build_author_job_query() -> _AuthorJobQuery:
    """Build the Job + resolved material-display SELECT (step A §4).

    Anchors and labels resolve by ``subject_type`` (KD12 polymorphic subject —
    no FK, so equality joins with a subject_type guard; PROBE-A / PROBE-A2):

    * ``authored_document`` (document_processing / document_preparation share it)
      → anchor = ``subject_id``, name = ``filename`` (one hop);
    * ``project_base`` → anchor = parent task id, name = parent ``filename`` via
      ``project_bases`` (two hops), plus ``version``;
    * ``course_node`` → no anchor, name = node ``title`` (one hop).

    NO ``deleted_at`` filter on any join — that is what makes P7 (deleted rows
    shown with their marker) work: the join sees the scrubbed source, not a NULL.
    """
    ad = aliased(AuthoredDocument)  # authored_document subject
    pb = aliased(ProjectBase)  # project_base subject
    pad = aliased(AuthoredDocument)  # the base's parent task
    cn = aliased(CourseNode)  # course_node subject

    is_doc = Job.subject_type == JOB_SUBJECT_TYPE[JobType.DOCUMENT_PROCESSING]
    is_base = Job.subject_type == JOB_SUBJECT_TYPE[JobType.BASE_NORMALIZE]
    is_node = Job.subject_type == JOB_SUBJECT_TYPE[JobType.NODE_SUMMARY_REGENERATION]

    material_anchor: ColumnElement[uuid.UUID | None] = case(
        (is_doc, Job.subject_id),
        (is_base, pb.authored_document_id),
        else_=None,
    )
    display_name = case(
        (is_doc, ad.filename),
        (is_base, pad.filename),
        (is_node, cn.title),
        else_=None,
    )
    material_deleted_at = case(
        (is_doc, ad.deleted_at),
        (is_base, pad.deleted_at),
        else_=None,
    )
    material_source_type = case(
        (is_doc, ad.source_type),
        (is_base, pad.source_type),
        else_=None,
    )

    stmt = (
        select(
            Job,
            material_anchor.label("material_id"),
            display_name.label("display_name"),
            material_deleted_at.label("material_deleted_at"),
            material_source_type.label("material_source_type"),
            pb.version.label("base_version"),
        )
        .select_from(Job)
        .outerjoin(ad, and_(ad.id == Job.subject_id, is_doc))
        .outerjoin(pb, and_(pb.id == Job.subject_id, is_base))
        .outerjoin(pad, pad.id == pb.authored_document_id)
        .outerjoin(cn, and_(cn.id == Job.subject_id, is_node))
    )
    return _AuthorJobQuery(select=stmt, material_anchor=material_anchor)


def _author_job_where(
    material_anchor: ColumnElement[uuid.UUID | None],
    *,
    tenant_id: uuid.UUID,
    state_class: StateClass | None,
    completed_after: datetime | None,
    material_id: uuid.UUID | None,
    job_types: frozenset[str],
) -> list[ColumnElement[bool]]:
    """Shared WHERE for door 1's list + count (step A §4)."""
    conds: list[ColumnElement[bool]] = [
        Job.tenant_id == tenant_id,
        Job.deleted_at.is_(None),
        Job.job_type.in_(job_types),
    ]
    if state_class == "in_flight":
        conds.append(Job.status.in_(IN_FLIGHT_STATUSES))
    elif state_class == "at_rest":
        conds.append(Job.status.in_(AT_REST_STATUSES))
    if completed_after is not None:
        # The lower time bound applies only to finished work; live work is never
        # filtered by time (§4) — an in-flight row always passes.
        conds.append(
            or_(
                Job.status.in_(IN_FLIGHT_STATUSES),
                Job.completed_at >= completed_after,
            )
        )
    if material_id is not None:
        # Catches both the document job (anchor = subject_id) and the base job
        # (anchor = parent task) of the same task (§4).
        conds.append(material_anchor == material_id)
    return conds


class JobRepository:
    """Repository for job tracking operations.

    Not tenant-scoped — jobs are accessed via node_id or directly by id.
    Tenant isolation is enforced via ``get_by_id_for_tenant`` which joins
    through ``CourseNode.tenant_id``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        tenant_id: uuid.UUID | None = None,
        course_node_id: uuid.UUID | None = None,
        job_type: JobType | str,
        priority: str = "normal",
        arq_job_id: str | None = None,
        input_params: dict[str, object] | None = None,
        duration_sec: float | None = None,
        subject_type: str | None = None,
        subject_id: uuid.UUID | None = None,
    ) -> Job:
        """Create a new job record.

        ``job_type`` accepts a :class:`JobType` enum (recommended) or a
        ``str`` equal to a canonical value; :func:`validate_job_type`
        raises ``ValueError`` on anything else, mirroring the DB CHECK
        ``ck_jobs_job_type`` with a clean write-site error.

        ``duration_sec`` is the intake-probed source video duration
        (DD-3.3c-I-B); NULL for non-video ingests and non-ingest jobs.
        It feeds :meth:`sum_active_ingest_duration_sec`.

        ``subject_type`` / ``subject_id`` (L1b) are the typed job subject —
        the enqueue helpers derive ``subject_type`` from
        :data:`~course_supporter.jobs.JOB_SUBJECT_TYPE` and pass the entity
        id as ``subject_id``. Both NULL for ``s3_cleanup`` (no natural
        subject). The DB enforces pair consistency (``ck_jobs_subject_pair``),
        the legal pair set (``ck_jobs_subject_type_legal``) and one-in-flight
        -per-subject (``uq_jobs_subject_in_flight``); a duplicate in-flight
        subject surfaces as ``IntegrityError`` (routes map it to a human 409).
        """
        job = Job(
            tenant_id=tenant_id,
            course_node_id=course_node_id,
            job_type=validate_job_type(job_type),
            priority=priority,
            arq_job_id=arq_job_id,
            input_params=input_params,
            duration_sec=duration_sec,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def get_by_id(self, job_id: uuid.UUID) -> Job | None:
        """Get a job by primary key."""
        stmt = select(Job).where(Job.id == job_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_inflight_job_for_subject(
        self, subject_type: str | None, subject_id: uuid.UUID | None
    ) -> Job | None:
        """Return an in-flight Job for the subject, or ``None`` (L1b).

        The application-level mirror of ``uq_jobs_subject_in_flight``: same
        predicate (``subject`` match + ``status IN IN_FLIGHT_STATUSES`` +
        ``deleted_at IS NULL``). Routes call this to raise a human 409 before
        an enqueue/reactivate would otherwise hit the DB index and surface a
        500 (contract invariant 5). The index — not this check — is the final
        judge under a concurrent race; this only buys a friendly message.

        A NULL subject (``s3_cleanup`` or an unrecoverable historical row)
        cannot conflict — NULLs are distinct in the partial-unique index — so
        this returns ``None`` immediately without a query.
        """
        if subject_type is None or subject_id is None:
            return None
        stmt = (
            select(Job)
            .where(
                Job.subject_type == subject_type,
                Job.subject_id == subject_id,
                Job.status.in_(IN_FLIGHT_STATUSES),
                Job.deleted_at.is_(None),
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_status(
        self,
        job_id: uuid.UUID,
        status: str,
        *,
        error_message: str | None = None,
        error_category: ErrorCategory | None = None,
        now: datetime | None = None,
    ) -> Job:
        """Transition job to a new status with validation.

        Args:
            error_category: Optional structural async-error code (F4);
                write-site validated (must be a security ErrorCategory
                member) and stored as its ``.value`` string.
            now: Override for current time (useful for testing).

        Raises:
            ValueError: If the transition is not allowed, or when
                ``error_category`` is not an ``ErrorCategory``.
        """
        job = await self.get_by_id(job_id)
        if job is None:
            msg = f"Job {job_id} not found"
            raise ValueError(msg)

        # Idempotent no-op when target equals current. ARQ task replay
        # (worker restart mid-await, ``max_tries`` retry, manual rerun)
        # would otherwise raise on ``active → active`` and crash the
        # second attempt before the real work runs (TASK-2.4.18).
        # Mirrors ``update_stage`` idempotency.
        if job.status == status:
            return job

        allowed = JOB_TRANSITIONS.get(job.status, set())
        if status not in allowed:
            msg = (
                f"Invalid job status transition: '{job.status}' → '{status}'. "
                f"Allowed: {allowed or 'none (terminal state)'}"
            )
            raise ValueError(msg)

        now = now or datetime.now(UTC)
        values: dict[str, object] = {"status": status}

        if status == "active":
            values["started_at"] = now
        elif status in ("complete", "failed"):
            values["completed_at"] = now
            if error_message is not None:
                values["error_message"] = error_message
            if error_category is not None:
                # Write-site validation (ratified Q4): the column is a
                # plain String; only enum members may reach it.
                if not isinstance(error_category, ErrorCategory):
                    msg = (
                        "error_category must be an ErrorCategory, "
                        f"got {error_category!r}"
                    )
                    raise ValueError(msg)
                values["error_category"] = error_category.value
        elif status == "cancelled":
            # ``completed_at`` on ``cancelled`` is the time of TERMINATION,
            # not of work (contract §3 "Meaning"). Kept a separate branch
            # from the complete/failed set so the two axes never merge: a
            # future terminal must not inherit completed_at by composition.
            values["completed_at"] = now
        elif status == "obsolete":
            # L2 seam terminal "subject vanished": the subject was soft-deleted
            # before/while the worker reached the job (skip-if-dead). Like
            # ``cancelled``, ``completed_at`` is the termination time, not work
            # time; unlike ``failed``, NO error fields — this is not a failure,
            # the world changed (Рат.1). A separate branch, not composed with
            # ``cancelled``: the two causes ("human cancelled" vs "subject
            # gone") stay distinguishable at the token.
            values["completed_at"] = now

        stmt = update(Job).where(Job.id == job_id).values(**values)
        await self._session.execute(stmt)
        await self._session.flush()
        # Re-fetch to get updated state
        updated = await self.get_by_id(job_id)
        if updated is None:  # pragma: no cover — guaranteed by prior flush
            msg = f"Job {job_id} disappeared after update"
            raise RuntimeError(msg)
        return updated

    async def set_arq_job_id(self, job_id: uuid.UUID, arq_job_id: str) -> None:
        """Set the ARQ job identifier after enqueue."""
        stmt = update(Job).where(Job.id == job_id).values(arq_job_id=arq_job_id)
        await self._session.execute(stmt)
        await self._session.flush()

    async def update_stage(self, job_id: uuid.UUID, stage_name: str) -> None:
        """Update the ``current_stage`` checkpoint marker on a live Job.

        Atomic UPDATE filtered through ``Job.deleted_at IS NULL`` so
        soft-deleted Jobs are skipped silently (KD13 + Phase 1
        soft-delete cascade discipline). No status transition
        validation -- stage taxonomy is per-pipeline free-form per
        :class:`Job.current_stage` column docs.

        Silent skip on:

        * Job not found (no row matches ``job_id``).
        * Job soft-deleted (``deleted_at IS NOT NULL``).

        Both cases mirror :meth:`set_arq_job_id` / :meth:`store_result`
        convention (void return; no exception). A ``log.warning`` is
        emitted on zero-rowcount for observability of caller race or
        wrong-id bugs without breaking the contract.

        Production callers added in Phase 2.1 C5 (Pass 2a entry), C6
        (Stage 2 entry), C7 (Pass 2b entry) per KD-2.1-B. Stage names
        live in ``config/ladders_*.yaml`` (KD16); this method accepts
        loose ``str`` and does no validation at write-time.
        """
        stmt = (
            update(Job)
            .where(Job.id == job_id, Job.deleted_at.is_(None))
            .values(current_stage=stage_name)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        # ``rowcount`` is provided by CursorResult (actual runtime type
        # for DML execute); SQLAlchemy's static return type is the wider
        # ``Result`` which does not expose it -- hence the ignore.
        if (result.rowcount or 0) == 0:  # type: ignore[attr-defined]
            logger.warning(
                "update_stage_no_job_found",
                job_id=str(job_id),
                stage_name=stage_name,
            )

    async def update_stage_progress(
        self, job_id: uuid.UUID, payload: dict[str, Any]
    ) -> None:
        """Full-replace ``Job.stage_progress`` JSONB (KD13 checkpoint).

        Atomic UPDATE filtered through ``Job.deleted_at IS NULL`` (same
        contract as :meth:`update_stage`). Silent skip on missing /
        soft-deleted row + warn-log for observability.

        **Full-replace is safe only because callers are strictly
        sequential** (Phase 3.2.2 visits await one another; no
        ``asyncio.gather`` over visits). Parallel visits would need
        JSON-merge instead of replace — a separate design decision,
        not a refactor. Any future temptation to fan-out node visits
        must surface as STOP-escalate first (K3 ratify, Phase 3.2.2
        commit 2).
        """
        stmt = (
            update(Job)
            .where(Job.id == job_id, Job.deleted_at.is_(None))
            .values(stage_progress=payload)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        if (result.rowcount or 0) == 0:  # type: ignore[attr-defined]
            logger.warning(
                "update_stage_progress_no_job_found",
                job_id=str(job_id),
            )

    async def reactivate(self, job_id: uuid.UUID) -> Job:
        """Re-queue a failed Job for retry (vision §3 KD13).

        Allowed only from ``status='failed'`` on a non-soft-deleted Job.
        Other states (including soft-deleted) raise :class:`ValueError`;
        callers map to 4xx (404 for missing, 409 for wrong state).

        DB-side transition only — caller is responsible for enqueuing
        the new ARQ task and calling :meth:`set_arq_job_id` with the
        new id, all within the same outer transaction. Mirrors the
        :meth:`create` / :meth:`set_arq_job_id` split used by
        ``enqueue_ingestion`` and friends.

        Cleared on transition: ``status`` (→ ``'queued'``),
        ``error_message``, ``started_at``, ``completed_at``,
        ``arq_job_id`` (caller will set the new one).

        Preserved on transition:

        * ``queued_at`` — original first-queued time is more meaningful
          as Job metadata; per-attempt timing lives in
          ``ExternalServiceCall`` per KD5/KD13.
        * ``stage_progress`` — worker resumes from KD4a checkpoint.
          This preservation is the design intent of ``reactivate``
          (vs creating a new Job from scratch).
        """
        job = await self.get_by_id(job_id)
        if job is None:
            msg = f"Job {job_id} not found"
            raise ValueError(msg)
        if job.deleted_at is not None:
            msg = f"Cannot reactivate soft-deleted Job {job_id}"
            raise ValueError(msg)
        if job.status != "failed":
            msg = (
                f"Cannot reactivate Job {job_id} in state {job.status!r}; "
                f"only 'failed' Jobs can be reactivated."
            )
            raise ValueError(msg)
        # Clear the per-retry fields here (reactivate keeps this) and route
        # the transition through ``update_status`` so ``Job.status`` has a
        # single writer. Generalising clear-on-requeue into ``update_status``
        # for one consumer would be a premature abstraction; the owner writes
        # only ``status``, reactivate writes only the non-status fields. The
        # field-clears autoflush before ``update_status``'s own re-fetch.
        job.error_message = None
        job.started_at = None
        job.completed_at = None
        job.arq_job_id = None
        return await self.update_status(job_id, "queued")

    async def store_result(
        self, job_id: uuid.UUID, result_data: dict[str, Any]
    ) -> None:
        """Store task result payload on the Job record."""
        stmt = update(Job).where(Job.id == job_id).values(result_data=result_data)
        await self._session.execute(stmt)
        await self._session.flush()

    async def get_by_id_for_tenant(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Job | None:
        """Get a job by ID, ensuring it belongs to the given tenant.

        Joins through ``job.course_node_id → material_node.tenant_id`` for
        isolation. Falls back to ``job.tenant_id`` for jobs without a
        linked node.
        """
        # Try node-based isolation first
        stmt = (
            select(Job)
            .join(CourseNode, Job.course_node_id == CourseNode.id)
            .where(Job.id == job_id, CourseNode.tenant_id == tenant_id)
        )
        result = await self._session.execute(stmt)
        job = result.scalar_one_or_none()
        if job is not None:
            return job

        # Fallback: direct tenant_id on job
        stmt = select(Job).where(Job.id == job_id, Job.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_in_flight_jobs(self) -> list[Job]:
        """Return all live Jobs in flight (``queued`` OR ``active``).

        Global (not tenant-scoped) and soft-delete-aware. Consumed by the worker
        startup reconcile sweep (task 3.3c-B; L2 extends it to ``queued`` — F11):
        a Job stranded in ``active`` (previous worker died mid-task) blocks the
        single serial worker's queue, and a Job stranded in ``queued`` with no
        live ARQ handle (e.g. a Redis flush) would otherwise sit invisible
        forever. The status set is derived from :data:`IN_FLIGHT_STATUSES`.
        """
        stmt = select(Job).where(
            Job.status.in_(IN_FLIGHT_STATUSES),
            Job.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def sum_active_ingest_duration_sec(self) -> float:
        """Sum intake-probed video duration over in-flight ingest jobs.

        Feeds the DD-3.3c-I-B admission gate, which compares the returned
        seconds against ``intake_admission_max_pending_video_hours``.
        Scope: ``job_type='document_processing'`` jobs whose status is
        in-flight (``queued`` — the document shows ``pending`` — or
        ``active`` — processing), excluding soft-deleted rows. NULL durations
        (non-video ingests, pre-DD-3.3c-I-B rows) are ignored by ``SUM``;
        ``COALESCE`` returns ``0.0`` for an empty queue.

        The status set is :data:`IN_FLIGHT_STATUSES` (``queued`` / ``active``)
        — there is no ``'pending'`` Job status (that is the AuthoredDocument
        state); the queued Job is what the worker has not yet picked up.
        """
        stmt = select(func.coalesce(func.sum(Job.duration_sec), 0.0)).where(
            Job.job_type == JobType.DOCUMENT_PROCESSING,
            Job.status.in_(IN_FLIGHT_STATUSES),
            Job.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        # COALESCE guarantees non-NULL at runtime; ``or 0.0`` satisfies mypy
        # (the summed column's static type stays float | None).
        return float(result.scalar_one() or 0.0)

    async def get_latest_ingest_duration_for_material(
        self, material_id: uuid.UUID
    ) -> float | None:
        """Return the most recent non-NULL ingest ``duration_sec`` for a material.

        Retry creates a fresh Job; rather than re-running the (network-heavy
        for URL) intake probe, it reuses the duration already probed by the
        original create/confirm. L1b: matches on the typed subject columns
        (``subject_type = 'authored_document' AND subject_id = material_id``),
        not JSONB containment. Returns ``None`` when no prior job carried a
        duration (legacy rows with NULL subject, or a genuine first probe) —
        the caller accepts a mild under-count over a re-probe.
        """
        stmt = (
            select(Job.duration_sec)
            .where(
                Job.subject_type == JOB_SUBJECT_TYPE[JobType.DOCUMENT_PROCESSING],
                Job.subject_id == material_id,
                Job.duration_sec.is_not(None),
            )
            .order_by(Job.queued_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Author work-list (step A §4) ──────────────────────────────────────

    async def list_author_jobs(
        self,
        tenant_id: uuid.UUID,
        *,
        state_class: StateClass | None = None,
        completed_after: datetime | None = None,
        material_id: uuid.UUID | None = None,
        job_types: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuthorJobRow]:
        """Flat page of author work (door 1, §4).

        Scope is ``Job.tenant_id`` (the denormalised column, stamped on every
        create path — PROBE-A q1); rows with a NULL owner fall out naturally.
        ``job_types`` is intersected with :data:`AUTHOR_JOB_TYPES` (the route
        rejects an out-of-universe value with 422; this is the defensive twin).
        Order is ``queued_at DESC`` then ``id`` (§4). Display fields resolve in
        the same query (join, not query-per-row).
        """
        effective = (
            AUTHOR_JOB_TYPES
            if job_types is None
            else frozenset(job_types) & AUTHOR_JOB_TYPES
        )
        q = _build_author_job_query()
        conds = _author_job_where(
            q.material_anchor,
            tenant_id=tenant_id,
            state_class=state_class,
            completed_after=completed_after,
            material_id=material_id,
            job_types=effective,
        )
        stmt = (
            q.select.where(*conds)
            .order_by(Job.queued_at.desc(), Job.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            AuthorJobRow(
                job=job,
                material_id=material_anchor,
                display_name=display_name,
                material_deleted_at=material_deleted_at,
                material_source_type=material_source_type,
                base_version=base_version,
            )
            for (
                job,
                material_anchor,
                display_name,
                material_deleted_at,
                material_source_type,
                base_version,
            ) in result.all()
        ]

    async def count_author_jobs(
        self,
        tenant_id: uuid.UUID,
        *,
        state_class: StateClass | None = None,
        completed_after: datetime | None = None,
        material_id: uuid.UUID | None = None,
        job_types: Sequence[str] | None = None,
    ) -> int:
        """Total author work matching the door-1 filters (§4; mirrors the
        ``list_roots`` / ``count_roots`` split)."""
        effective = (
            AUTHOR_JOB_TYPES
            if job_types is None
            else frozenset(job_types) & AUTHOR_JOB_TYPES
        )
        q = _build_author_job_query()
        conds = _author_job_where(
            q.material_anchor,
            tenant_id=tenant_id,
            state_class=state_class,
            completed_after=completed_after,
            material_id=material_id,
            job_types=effective,
        )
        stmt = select(func.count()).select_from(q.select.where(*conds).subquery())
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def list_author_material_history(
        self,
        tenant_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MaterialHistoryRow]:
        """Page of materials with their work history (door 2, §4).

        Grouping is done entirely in SQL so a page boundary never splits a
        group. Three fixed queries per page (a constant cost — never query-per-
        row):

        1. the windowed grouping over the material anchor — per anchor the latest
           job id (``row_number`` by ``queued_at DESC``), the work count, and the
           last-activity moment ``MAX(COALESCE(completed_at, queued_at))``,
           ordered by last activity and paged;
        2. the latest jobs' own display fields, loaded through the shared door-1
           join keyed on the page's job ids (renders each ``last_job``);
        3. the anchor materials with ``pending_job`` eager-loaded so
           :attr:`AuthoredDocument.processing_phase` does not trip
           ``PendingJobNotLoadedError``.

        node_summary work is excluded (no material anchor; history is materials —
        decision P6). The §4 sketch estimated two queries; splitting the last-job
        display off the grouping keeps that query a plain window and reuses the
        door-1 join verbatim — the constant-per-page guarantee is unchanged.
        """
        pb = aliased(ProjectBase)
        anchor: ColumnElement[uuid.UUID | None] = case(
            (
                Job.subject_type == JOB_SUBJECT_TYPE[JobType.DOCUMENT_PROCESSING],
                Job.subject_id,
            ),
            (
                Job.subject_type == JOB_SUBJECT_TYPE[JobType.BASE_NORMALIZE],
                pb.authored_document_id,
            ),
            else_=None,
        )
        ranked = (
            select(
                Job.id.label("job_id"),
                anchor.label("anchor_id"),
                func.count().over(partition_by=anchor).label("jobs_count"),
                func.max(func.coalesce(Job.completed_at, Job.queued_at))
                .over(partition_by=anchor)
                .label("last_activity"),
                func.row_number()
                .over(
                    partition_by=anchor, order_by=(Job.queued_at.desc(), Job.id.desc())
                )
                .label("rn"),
            )
            .select_from(Job)
            .outerjoin(
                pb,
                and_(pb.id == Job.subject_id, Job.subject_type == "project_base"),
            )
            .where(
                Job.tenant_id == tenant_id,
                Job.deleted_at.is_(None),
                Job.job_type.in_(AUTHOR_HISTORY_JOB_TYPES),
                anchor.is_not(None),
            )
        ).subquery()

        page_stmt = (
            select(
                ranked.c.anchor_id,
                ranked.c.job_id,
                ranked.c.jobs_count,
            )
            .where(ranked.c.rn == 1)
            .order_by(ranked.c.last_activity.desc(), ranked.c.anchor_id)
            .limit(limit)
            .offset(offset)
        )
        page = (await self._session.execute(page_stmt)).all()
        if not page:
            return []

        anchor_ids = [anchor_id for anchor_id, _job_id, _count in page]
        last_job_ids = [job_id for _anchor_id, job_id, _count in page]

        job_query = _build_author_job_query().select.where(Job.id.in_(last_job_ids))
        job_rows: dict[uuid.UUID, AuthorJobRow] = {
            job.id: AuthorJobRow(
                job=job,
                material_id=material_anchor,
                display_name=display_name,
                material_deleted_at=material_deleted_at,
                material_source_type=material_source_type,
                base_version=base_version,
            )
            for (
                job,
                material_anchor,
                display_name,
                material_deleted_at,
                material_source_type,
                base_version,
            ) in (await self._session.execute(job_query)).all()
        }

        # All anchors are authored_documents.id (doc subject, or a base's parent
        # task). Load them WITHOUT a deleted filter (a deleted anchor still shows
        # as a marker row) with pending_job eager for the phase property.
        mats_stmt = (
            select(AuthoredDocument)
            .where(AuthoredDocument.id.in_(anchor_ids))
            .options(selectinload(AuthoredDocument.pending_job))
        )
        mats: dict[uuid.UUID, AuthoredDocument] = {
            ad.id: ad for ad in (await self._session.execute(mats_stmt)).scalars().all()
        }

        history: list[MaterialHistoryRow] = []
        for anchor_id, job_id, jobs_count in page:
            ad = mats.get(anchor_id)
            # Deleted material is a marker, not a phase (decision P7): skip the
            # property entirely for a deleted (or missing) anchor.
            phase = (
                None if ad is None or ad.deleted_at is not None else ad.processing_phase
            )
            history.append(
                MaterialHistoryRow(
                    material_id=anchor_id,
                    jobs_count=jobs_count,
                    last_job=job_rows[job_id],
                    display_name=ad.filename if ad is not None else None,
                    material_source_type=ad.source_type if ad is not None else None,
                    material_deleted_at=ad.deleted_at if ad is not None else None,
                    processing_phase=phase,
                )
            )
        return history

    async def count_author_material_anchors(self, tenant_id: uuid.UUID) -> int:
        """Count distinct material anchors in the author history (door 2, §4)."""
        pb = aliased(ProjectBase)
        anchor: ColumnElement[uuid.UUID | None] = case(
            (
                Job.subject_type == JOB_SUBJECT_TYPE[JobType.DOCUMENT_PROCESSING],
                Job.subject_id,
            ),
            (
                Job.subject_type == JOB_SUBJECT_TYPE[JobType.BASE_NORMALIZE],
                pb.authored_document_id,
            ),
            else_=None,
        )
        stmt = (
            select(func.count(distinct(anchor)))
            .select_from(Job)
            .outerjoin(
                pb,
                and_(pb.id == Job.subject_id, Job.subject_type == "project_base"),
            )
            .where(
                Job.tenant_id == tenant_id,
                Job.deleted_at.is_(None),
                Job.job_type.in_(AUTHOR_HISTORY_JOB_TYPES),
                anchor.is_not(None),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()
