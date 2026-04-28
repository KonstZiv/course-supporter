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
    ``course_node_id`` (recursive via ``parent_materialnode_id``).
    """

    model_config = ConfigDict(populate_by_name=True)

    course_node_id: UUID
    course_title: str
    from_: date = Field(alias="from")
    to_: date = Field(alias="to")
    total_usd: float
    by_node: list[ByNodeEntry]
    by_action: list[ByActionEntry]
