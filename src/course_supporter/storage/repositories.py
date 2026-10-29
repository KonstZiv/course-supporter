"""CRUD repositories for database operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import ColumnElement, Row, String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.cost_kinds import CostKind
from course_supporter.jobs import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    SoftDeleteMixin,
    Student,
    StudentCredential,
)


class SoftDeleteRepository[ModelT: SoftDeleteMixin]:
    """Base repository for entities with the soft-delete pattern.

    Provides three filtered listing flavors and an idempotent
    ``soft_delete`` setter (vision §3 KD3, KD12). Subclasses bind a
    concrete model by setting the ``model`` class attribute:

        class TenantRepository(SoftDeleteRepository[Tenant]):
            model = Tenant

    Methods do not commit; the caller controls the transaction
    boundary, matching the project's existing repository style.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_active(self) -> list[ModelT]:
        """List rows where ``deleted_at IS NULL``."""
        stmt = select(self.model).where(self.model.deleted_at.is_(None))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_including_deleted(self) -> list[ModelT]:
        """List all rows regardless of soft-delete state.

        Use for audit, cost attribution, and other read paths that
        must see historical entities.
        """
        stmt = select(self.model)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_only_deleted(self) -> list[ModelT]:
        """List rows where ``deleted_at IS NOT NULL``.

        Use for orphan cleanup tasks (e.g., S3 hard-delete sweeper).
        """
        stmt = select(self.model).where(self.model.deleted_at.is_not(None))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def soft_delete(
        self,
        entity_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> ModelT | None:
        """Set ``deleted_at`` on the row identified by ``entity_id``.

        Returns the entity (or ``None`` if not found). Idempotent —
        re-calling on an already-deleted row is a no-op and does not
        trigger an UPDATE (which would be blocked by the DB-level
        soft-delete protection trigger).
        """
        entity = await self._session.get(self.model, entity_id)
        if entity is None:
            return None
        if entity.deleted_at is not None:
            return entity
        entity.deleted_at = now if now is not None else datetime.now(UTC)
        await self._session.flush()
        return entity


class CostSums(NamedTuple):
    """A cost figure split by the kind of its rows' sums (BIL-C, decision 7).

    Exact and estimated dollars are summed apart; calls of unknown cost have no
    sum, so they are counted. ``cost_usd`` is the one money figure the reports
    have always shown: exact plus estimated — the register's best number, the
    same one the funds port is told for a stage.
    """

    exact_usd: float = 0.0
    estimated_usd: float = 0.0
    unknown_calls: int = 0

    @property
    def cost_usd(self) -> float:
        return self.exact_usd + self.estimated_usd


class ByCourseRow(NamedTuple):
    """One course row in /cost/summary by_course breakdown."""

    course_node_id: uuid.UUID
    course_title: str
    cost: CostSums


class ByProviderRow(NamedTuple):
    """One (provider, model_id) row in /cost/summary by_provider breakdown."""

    provider: str
    model_id: str
    cost: CostSums


class ByNodeRow(NamedTuple):
    """One node row in /cost/course/{id} by_node breakdown."""

    course_node_id: uuid.UUID
    title: str
    cost: CostSums


class ByActionRow(NamedTuple):
    """One action row in /cost/course/{id} by_action breakdown."""

    action: str
    cost: CostSums


class HomeworkByCourseRow(NamedTuple):
    """One course root in the /cost/homework by_course breakdown."""

    course_node_id: uuid.UUID
    course_title: str
    cost: CostSums


class HomeworkByTaskRow(NamedTuple):
    """One task (AuthoredDocument) in the /cost/homework/course/{id} breakdown.

    Carries the raw label fields (``title``/``filename``/``source_type``/
    ``order``) rather than a composed label: the display label is composed in
    the route layer via the shared ``material_label`` helper (an api-layer
    projection the storage layer must not import). ``is_deleted`` flags a
    soft-deleted task — shown, not filtered (its processing cost still counts).
    """

    authored_document_id: uuid.UUID
    title: str | None
    filename: str | None
    source_type: str
    order: int
    is_deleted: bool
    cost: CostSums


class HomeworkByStudentRow(NamedTuple):
    """One student in the /cost/homework/task/{id} leaf breakdown.

    ``student_display`` is resolved server-side via COALESCE(display_name,
    credential login, external_id, id) so a mode-1 student with a NULL
    display_name still renders a stable identifier.
    """

    student_id: uuid.UUID
    student_display: str
    cost: CostSums


def _sum_columns() -> list[ColumnElement[Any]]:
    """The three aggregates every report selects: exact, estimated, unknown."""
    cost = ExternalServiceCall.cost_usd
    kind = ExternalServiceCall.cost_kind
    return [
        func.coalesce(func.sum(cost).filter(kind == CostKind.EXACT.value), 0).label(
            "exact_usd"
        ),
        func.coalesce(func.sum(cost).filter(kind == CostKind.ESTIMATED.value), 0).label(
            "estimated_usd"
        ),
        func.count().filter(kind == CostKind.UNKNOWN.value).label("unknown_calls"),
    ]


def _sums(row: Row[Any]) -> CostSums:
    return CostSums(
        exact_usd=float(row.exact_usd),
        estimated_usd=float(row.estimated_usd),
        unknown_calls=int(row.unknown_calls),
    )


# Rows of a paid call — the ones a cost report reads. A row with no kind
# records no paid call (a trace, metrics, the funds port, a webhook).
_PAID_CALL = ExternalServiceCall.cost_kind.is_not(None)
# Dearest first; a group with only unknown sums has no money and sorts last.
_DEAREST_FIRST = func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0).desc()

# The homework submission a job is about (BIL-C, decision 5): every job whose
# subject is the submission — the first one and every continuation — not the
# one ``homework_submissions.job_id`` happens to point at now.
_JOB_OF_SUBMISSION = and_(
    Job.subject_type == JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING],
    Job.subject_id == HomeworkSubmission.id,
)


def _to_exclusive(to_date: date) -> date:
    """Convert inclusive end-date to exclusive (``< to + 1 day``).

    The endpoint contract is inclusive on both bounds (``from..to`` covers
    all of ``to``-day). Implementing as ``created_at >= :from AND
    created_at < :to_exclusive`` cleanly handles the time component of
    ``timestamptz`` without timezone gymnastics.
    """
    return to_date + timedelta(days=1)


class ExternalServiceCallRepository:
    """Repository for ExternalServiceCall queries (KD5).

    Cost-reporting query methods that materialise into the Pydantic
    response models declared in :mod:`course_supporter.models.cost`.
    All methods are read-only aggregations; ESC writes flow through
    :func:`course_supporter.service_logging._persist` (single write
    surface) plus :func:`~course_supporter.service_logging.add_register_row`
    for webhook deliveries, which share its job check.

    Common SQL invariants across the breakdown methods:

    * Tenant scope via ``JOIN jobs ON ESC.job_id = jobs.id WHERE
      jobs.tenant_id = :tenant_id`` — KD5 dropped ``ESC.tenant_id``,
      attribution flows through Job.
    * Date range ``ESC.created_at >= :from AND ESC.created_at <
      :to_exclusive`` where ``:to_exclusive = :to + 1 day`` (inclusive
      end-of-day).
    * ``WHERE cost_kind IS NOT NULL`` — the rows of paid calls (BIL-C). Every
      figure is :class:`CostSums`: exact and estimated dollars summed apart,
      calls of unknown cost counted (their sum is NULL, never a zero). Rows
      that record no paid call (a skipped / abandoned ladder rung, the
      per-review metrics row, the funds-port row, a webhook delivery) carry
      no kind and are not read.
    * ``ORDER BY COALESCE(SUM(cost_usd), 0) DESC`` on every breakdown —
      cost-priority first, predictable for UI; a group of unknown sums only
      sorts last.
    * Pagination (``LIMIT/OFFSET``) only on aggregated breakdowns;
      scalar totals are not paginated.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_total_for_period(
        self,
        *,
        tenant_id: uuid.UUID,
        from_date: date,
        to_date: date,
    ) -> CostSums:
        """Sum of attributed and unattributed ESC costs in the period.

        Includes Jobs with ``course_node_id IS NULL`` (orphan jobs).
        All zero if no rows match.
        """
        stmt = (
            select(*_sum_columns())
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .where(
                Job.tenant_id == tenant_id,
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
        )
        result = await self._session.execute(stmt)
        return _sums(result.one())

    async def get_total_for_subtree(
        self,
        *,
        tenant_id: uuid.UUID,
        course_node_ids: Sequence[uuid.UUID],
        from_date: date,
        to_date: date,
    ) -> CostSums:
        """Sum of ESC costs for Jobs whose ``course_node_id`` is in subtree.

        Per-course total for ``/cost/course/{id}`` — distinct from
        :meth:`get_total_for_period` (tenant-wide). Empty
        ``course_node_ids`` short-circuits to zero (no SQL emitted).

        Performance ceiling: ``course_node_ids`` is consumed as
        ``Job.course_node_id IN (...)``. PostgreSQL planner heuristics
        start to degrade with IN-lists ≳1000 elements (parse cost,
        planner exploration time). Decision lever: if a tenant ships
        a course with ≥500 descendant nodes, run ``EXPLAIN ANALYZE``
        on ``/cost/course/{root}``; if a sequential scan is chosen or
        IN-list parse latency dominates, inline the recursive
        descendant CTE into the cost query (single round-trip,
        eliminates IN-list size as a factor). Producer side note in
        :meth:`CourseNodeRepository.get_descendant_ids`.
        """
        if not course_node_ids:
            return CostSums()
        stmt = (
            select(*_sum_columns())
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .where(
                Job.tenant_id == tenant_id,
                Job.course_node_id.in_(course_node_ids),
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
        )
        result = await self._session.execute(stmt)
        return _sums(result.one())

    async def get_unattributed_for_period(
        self,
        *,
        tenant_id: uuid.UUID,
        from_date: date,
        to_date: date,
    ) -> CostSums:
        """Sum of ESC costs for Jobs with ``course_node_id IS NULL``.

        Invariant on the route layer, kind by kind:
        ``total == sum(by_course) + unattributed``.
        All zero if no rows match.
        """
        stmt = (
            select(*_sum_columns())
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .where(
                Job.tenant_id == tenant_id,
                Job.course_node_id.is_(None),
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
        )
        result = await self._session.execute(stmt)
        return _sums(result.one())

    async def get_by_course(
        self,
        *,
        tenant_id: uuid.UUID,
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ByCourseRow]:
        """Cost grouped by ``Job.course_node_id`` (Variant D — direct).

        Groups directly by whichever node the Job points at; if Jobs
        target sub-course nodes (e.g. lessons), they appear as their
        own rows. Course-root aggregation is the responsibility of the
        UI client. See POST-MR-NOTES forward-looking note for the
        future Phase 1+ walk-up CTE option.

        Excludes orphan Jobs (``course_node_id IS NULL``) — those go
        into ``unattributed`` via :meth:`get_unattributed_for_period`.
        """
        stmt = (
            select(
                CourseNode.id.label("course_node_id"),
                CourseNode.title.label("course_title"),
                *_sum_columns(),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .join(CourseNode, Job.course_node_id == CourseNode.id)
            .where(
                Job.tenant_id == tenant_id,
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
            .group_by(CourseNode.id, CourseNode.title)
            .order_by(_DEAREST_FIRST)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            ByCourseRow(
                course_node_id=row.course_node_id,
                course_title=row.course_title,
                cost=_sums(row),
            )
            for row in result.all()
        ]

    async def get_by_provider(
        self,
        *,
        tenant_id: uuid.UUID,
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ByProviderRow]:
        """Cost grouped by ``(ESC.provider, ESC.model_id)``.

        Includes Jobs with ``course_node_id IS NULL`` — provider-level
        breakdown is independent of course attribution.
        """
        stmt = (
            select(
                ExternalServiceCall.provider.label("provider"),
                ExternalServiceCall.model_id.label("model_id"),
                *_sum_columns(),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .where(
                Job.tenant_id == tenant_id,
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
            .group_by(ExternalServiceCall.provider, ExternalServiceCall.model_id)
            .order_by(_DEAREST_FIRST)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            ByProviderRow(
                provider=row.provider,
                model_id=row.model_id,
                cost=_sums(row),
            )
            for row in result.all()
        ]

    async def get_by_node(
        self,
        *,
        tenant_id: uuid.UUID,
        course_node_ids: Sequence[uuid.UUID],
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ByNodeRow]:
        """Cost grouped by node within a pre-resolved subtree.

        ``course_node_ids`` is the descendant set of the requested course
        root, resolved on the route layer via
        :meth:`CourseNodeRepository.get_descendant_ids`. Empty list →
        empty result (no SQL).

        Performance ceiling: ``course_node_ids`` is consumed as
        ``Job.course_node_id IN (...)``. PostgreSQL planner heuristics
        start to degrade with IN-lists ≳1000 elements (parse cost,
        planner exploration time). Decision lever: if a tenant ships
        a course with ≥500 descendant nodes, run ``EXPLAIN ANALYZE``
        on ``/cost/course/{root}``; if a sequential scan is chosen or
        IN-list parse latency dominates, inline the recursive
        descendant CTE into the cost query (single round-trip,
        eliminates IN-list size as a factor). Producer side note in
        :meth:`CourseNodeRepository.get_descendant_ids`.
        """
        if not course_node_ids:
            return []
        stmt = (
            select(
                CourseNode.id.label("course_node_id"),
                CourseNode.title.label("title"),
                *_sum_columns(),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .join(CourseNode, Job.course_node_id == CourseNode.id)
            .where(
                Job.tenant_id == tenant_id,
                Job.course_node_id.in_(course_node_ids),
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
            .group_by(CourseNode.id, CourseNode.title)
            .order_by(_DEAREST_FIRST)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            ByNodeRow(
                course_node_id=row.course_node_id,
                title=row.title,
                cost=_sums(row),
            )
            for row in result.all()
        ]

    async def get_by_action(
        self,
        *,
        tenant_id: uuid.UUID,
        course_node_ids: Sequence[uuid.UUID],
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ByActionRow]:
        """Cost grouped by ``ESC.action`` within a pre-resolved subtree.

        Same subtree contract as :meth:`get_by_node`. Empty list →
        empty result.

        Performance ceiling: ``course_node_ids`` is consumed as
        ``Job.course_node_id IN (...)``. PostgreSQL planner heuristics
        start to degrade with IN-lists ≳1000 elements (parse cost,
        planner exploration time). Decision lever: if a tenant ships
        a course with ≥500 descendant nodes, run ``EXPLAIN ANALYZE``
        on ``/cost/course/{root}``; if a sequential scan is chosen or
        IN-list parse latency dominates, inline the recursive
        descendant CTE into the cost query (single round-trip,
        eliminates IN-list size as a factor). Producer side note in
        :meth:`CourseNodeRepository.get_descendant_ids`.
        """
        if not course_node_ids:
            return []
        stmt = (
            select(
                ExternalServiceCall.action.label("action"),
                *_sum_columns(),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .where(
                Job.tenant_id == tenant_id,
                Job.course_node_id.in_(course_node_ids),
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
            .group_by(ExternalServiceCall.action)
            .order_by(_DEAREST_FIRST)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [ByActionRow(action=row.action, cost=_sums(row)) for row in result.all()]


class HomeworkCostRepository:
    """Read-only cost attribution for homework processing (Phase 6, 6.HC).

    A cross-section distinct from the ingestion-oriented
    :class:`ExternalServiceCallRepository`. Homework ``ExternalServiceCall``
    rows are written by the same ``service_logging._persist`` surface, but the
    homework ``Job`` is created with ``course_node_id = NULL`` (Variant B
    rejected — DD-6-I), so ``/cost/summary`` files their cost under
    ``unattributed`` and never under ``by_course``. This repository attributes
    them through the job's SUBJECT (BIL-C, decision 5): ESC → ``jobs`` →
    the ``HomeworkSubmission`` whose id is the job's ``subject_id``. Every job
    of the submission counts — the first one and each continuation — where the
    former join on ``homework_submissions.job_id`` saw only the latest (each
    continuation overwrites that column). The submission carries every
    grouping key (tenant, student, task, course-root, node, time). The inner
    joins drop ingestion ESC (whose job is about no submission).

    Shares the :class:`ExternalServiceCallRepository` invariants:
    :class:`CostSums` figures, ``WHERE cost_kind IS NOT NULL``, inclusive date
    range via :func:`_to_exclusive`, dearest first, pagination on the
    breakdowns.

    Tenant isolation: every query filters ``HomeworkSubmission.tenant_id``
    (NOT NULL) — never ``Job.tenant_id`` (nullable) and never through
    ``Student`` (not a tenant boundary). The leak surface is zero.

    Soft-delete: enrichment joins (``CourseNode`` / ``AuthoredDocument`` /
    ``Student``) are by id with NO ``deleted_at`` filter, so a soft-deleted
    course, task, or student still resolves its name and keeps its cost. Task
    deletion is surfaced via ``HomeworkByTaskRow.is_deleted`` rather than
    hidden.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_homework_total(
        self,
        *,
        tenant_id: uuid.UUID,
        from_date: date,
        to_date: date,
    ) -> CostSums:
        """Total homework-processing cost for the tenant in the period.

        Level 1 (top). All zero if no rows match.
        """
        stmt = (
            select(*_sum_columns())
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .join(HomeworkSubmission, _JOB_OF_SUBMISSION)
            .where(
                HomeworkSubmission.tenant_id == tenant_id,
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
        )
        result = await self._session.execute(stmt)
        return _sums(result.one())

    async def get_homework_by_course(
        self,
        *,
        tenant_id: uuid.UUID,
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HomeworkByCourseRow]:
        """Homework cost grouped by course root (level 1 breakdown).

        Groups by ``HomeworkSubmission.course_node_id`` (the course ROOT,
        KD15) and enriches ``CourseNode.title``. The parent-key drill for
        level 2 is each row's ``course_node_id``.
        """
        stmt = (
            select(
                CourseNode.id.label("course_node_id"),
                CourseNode.title.label("course_title"),
                *_sum_columns(),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .join(HomeworkSubmission, _JOB_OF_SUBMISSION)
            .join(CourseNode, HomeworkSubmission.course_node_id == CourseNode.id)
            .where(
                HomeworkSubmission.tenant_id == tenant_id,
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
            .group_by(CourseNode.id, CourseNode.title)
            .order_by(_DEAREST_FIRST)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            HomeworkByCourseRow(
                course_node_id=row.course_node_id,
                course_title=row.course_title,
                cost=_sums(row),
            )
            for row in result.all()
        ]

    async def get_homework_by_task(
        self,
        *,
        tenant_id: uuid.UUID,
        course_node_id: uuid.UUID,
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HomeworkByTaskRow]:
        """Homework cost grouped by task within one course (level 2 breakdown).

        Scoped to one course root (``course_node_id``) and grouped by
        ``authored_document_id``. Soft-deleted tasks are INCLUDED — the
        ``AuthoredDocument`` join is by id with no ``deleted_at`` filter, so a
        soft-deleted task keeps its row and cost; ``is_deleted`` flags it for
        the UI marker. Raw label fields are returned for the route to compose
        the T4a-identical display label.
        """
        stmt = (
            select(
                AuthoredDocument.id.label("authored_document_id"),
                AuthoredDocument.title.label("title"),
                AuthoredDocument.filename.label("filename"),
                AuthoredDocument.source_type.label("source_type"),
                AuthoredDocument.order.label("order"),
                AuthoredDocument.deleted_at.label("deleted_at"),
                *_sum_columns(),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .join(HomeworkSubmission, _JOB_OF_SUBMISSION)
            .join(
                AuthoredDocument,
                HomeworkSubmission.authored_document_id == AuthoredDocument.id,
            )
            .where(
                HomeworkSubmission.tenant_id == tenant_id,
                HomeworkSubmission.course_node_id == course_node_id,
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
            .group_by(
                AuthoredDocument.id,
                AuthoredDocument.title,
                AuthoredDocument.filename,
                AuthoredDocument.source_type,
                AuthoredDocument.order,
                AuthoredDocument.deleted_at,
            )
            .order_by(_DEAREST_FIRST)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            HomeworkByTaskRow(
                authored_document_id=row.authored_document_id,
                title=row.title,
                filename=row.filename,
                source_type=row.source_type,
                order=row.order,
                is_deleted=row.deleted_at is not None,
                cost=_sums(row),
            )
            for row in result.all()
        ]

    async def get_homework_by_student(
        self,
        *,
        tenant_id: uuid.UUID,
        authored_document_id: uuid.UUID,
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HomeworkByStudentRow]:
        """Homework cost grouped by student for one task — the leaf (level 3).

        The leaf sums cost across ALL of the student's submissions for the task
        (every job of every submission → its ESC rows). ``student_display``
        falls back display_name → credential login → external_id → id so a
        mode-1 student with no portal display_name and no credential still
        resolves. Student is inner-joined (``student_id`` is NOT NULL); the
        credential is outer-joined (1:1-optional).
        """
        student_display = func.coalesce(
            Student.display_name,
            StudentCredential.login,
            Student.external_id,
            cast(Student.id, String),
        )
        stmt = (
            select(
                Student.id.label("student_id"),
                student_display.label("student_display"),
                *_sum_columns(),
            )
            .select_from(ExternalServiceCall)
            .join(Job, ExternalServiceCall.job_id == Job.id)
            .join(HomeworkSubmission, _JOB_OF_SUBMISSION)
            .join(Student, HomeworkSubmission.student_id == Student.id)
            .outerjoin(StudentCredential, StudentCredential.student_id == Student.id)
            .where(
                HomeworkSubmission.tenant_id == tenant_id,
                HomeworkSubmission.authored_document_id == authored_document_id,
                ExternalServiceCall.created_at >= from_date,
                ExternalServiceCall.created_at < _to_exclusive(to_date),
                _PAID_CALL,
            )
            .group_by(
                Student.id,
                Student.display_name,
                Student.external_id,
                StudentCredential.login,
            )
            .order_by(_DEAREST_FIRST)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [
            HomeworkByStudentRow(
                student_id=row.student_id,
                student_display=row.student_display,
                cost=_sums(row),
            )
            for row in result.all()
        ]
