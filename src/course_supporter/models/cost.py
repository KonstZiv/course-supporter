"""Pydantic response models for cost-reporting endpoints (KD5).

Pure DTOs — no ORM dependencies; the cost-query repository methods
(commit (c)) materialise rows into these shapes for serialisation.

The KD5 endpoint contract uses ``from``/``to`` as JSON keys for the
date range. Both are Python keywords, so the field names are
``from_``/``to_`` with ``Field(alias=...)`` to surface the canonical
JSON keys. FastAPI's ``response_model_by_alias=True`` default makes
this transparent on the wire.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ByCourseEntry(BaseModel):
    """One course root in the by-course breakdown of /cost/summary."""

    course_node_id: UUID
    course_title: str
    cost_usd: float


class ByProviderEntry(BaseModel):
    """One (provider, model) pair in the by-provider breakdown."""

    provider: str
    model_id: str
    cost_usd: float


class ByNodeEntry(BaseModel):
    """One descendant node in the by-node breakdown of /cost/course/{id}."""

    course_node_id: UUID
    title: str
    cost_usd: float


class ByActionEntry(BaseModel):
    """One action label in the by-action breakdown of /cost/course/{id}."""

    action: str
    cost_usd: float


class CostSummaryResponse(BaseModel):
    """Response shape for ``GET /api/v1/cost/summary``.

    Invariant: ``total_usd == sum(by_course.cost_usd) + unattributed_cost_usd``.
    ``unattributed_cost_usd`` aggregates ESCs whose ``Job.course_node_id``
    is NULL (orphan jobs without a course attribution).
    """

    model_config = ConfigDict(populate_by_name=True)

    from_: date = Field(alias="from")
    to_: date = Field(alias="to")
    total_usd: float
    unattributed_cost_usd: float
    by_course: list[ByCourseEntry]
    by_provider: list[ByProviderEntry]


class CourseCostResponse(BaseModel):
    """Response shape for ``GET /api/v1/cost/course/{course_node_id}``.

    Drill-down: ``by_node`` covers the entire subtree rooted at
    ``course_node_id`` (recursive via ``parent_id``).
    """

    model_config = ConfigDict(populate_by_name=True)

    course_node_id: UUID
    course_title: str
    from_: date = Field(alias="from")
    to_: date = Field(alias="to")
    total_usd: float
    by_node: list[ByNodeEntry]
    by_action: list[ByActionEntry]


# ── Homework cost-attribution (Phase 6, 6.HC) ──
#
# A separate surface from /cost/summary: the homework Job carries
# course_node_id=NULL (Variant B rejected, DD-6-I), so homework cost lands in
# /cost/summary's unattributed bucket. These three responses form the
# on-demand drill tree total → by-course → by-task → by-student, each level a
# separate request keyed by the parent (the 4th "task total" is already a row
# in the course response, so there is no standalone task-aggregate endpoint).


class HomeworkByCourseEntry(BaseModel):
    """One course root in the by-course breakdown of /cost/homework."""

    course_node_id: UUID
    course_title: str
    cost_usd: float


class HomeworkByTaskEntry(BaseModel):
    """One task in the by-task breakdown of /cost/homework/course/{id}.

    ``is_deleted`` marks a soft-deleted task (``AuthoredDocument.deleted_at``):
    shown, not filtered — its processing cost still counts. ``task_label`` is
    the T4a-identical ``filename | {source_type} #{order}`` label.
    """

    authored_document_id: UUID
    task_label: str
    is_deleted: bool
    cost_usd: float


class HomeworkByStudentEntry(BaseModel):
    """One student in the leaf breakdown of /cost/homework/task/{id}.

    ``cost_usd`` sums the student's cost across ALL their submissions for the
    task. ``student_display`` falls back display_name → login → external_id →
    id so mode-1 students without a portal display_name still render.
    """

    student_id: UUID
    student_display: str
    cost_usd: float


class HomeworkCostResponse(BaseModel):
    """Response shape for ``GET /api/v1/cost/homework`` (level 1).

    Tenant-wide homework-processing cost: a period total plus the by-course
    breakdown. ``by_course`` may be paginated, so ``total_usd`` stays whole
    (does not necessarily equal the sum of the returned page).
    """

    model_config = ConfigDict(populate_by_name=True)

    from_: date = Field(alias="from")
    to_: date = Field(alias="to")
    total_usd: float
    by_course: list[HomeworkByCourseEntry]


class HomeworkCourseCostResponse(BaseModel):
    """Response shape for ``GET /api/v1/cost/homework/course/{course_node_id}``.

    Level 2: the tasks of one course with their homework-processing cost. Each
    task's ``cost_usd`` is the level-2 "task total" (so no separate
    task-aggregate endpoint is needed). Drill into a task via its
    ``authored_document_id``.
    """

    model_config = ConfigDict(populate_by_name=True)

    course_node_id: UUID
    from_: date = Field(alias="from")
    to_: date = Field(alias="to")
    by_task: list[HomeworkByTaskEntry]


class HomeworkTaskCostResponse(BaseModel):
    """Response shape for ``GET /api/v1/cost/homework/task/{authored_document_id}``.

    Level 3 (leaf): the students who submitted the task, each with their
    summed homework-processing cost across all their attempts.
    """

    model_config = ConfigDict(populate_by_name=True)

    authored_document_id: UUID
    from_: date = Field(alias="from")
    to_: date = Field(alias="to")
    by_student: list[HomeworkByStudentEntry]
