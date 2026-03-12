"""Reconciliation preview models for field-level issue detection."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ReconciliationIssueType(StrEnum):
    """Category of cross-node consistency issue."""

    CONTRADICTION = "contradiction"
    GAP = "gap"
    OVERLAP = "overlap"
    INCONSISTENCY = "inconsistency"


class ReconciliationIssue(BaseModel):
    """Single field-level issue detected during reconciliation preview."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    editable_node_id: uuid.UUID
    node_title: str
    field: str
    issue_type: ReconciliationIssueType
    description: str
    current_value: Any = None
    suggested_value: Any = None
    reasoning: str


class ReconciliationPreview(BaseModel):
    """Full preview result from reconciliation analysis."""

    issues: list[ReconciliationIssue] = Field(default_factory=list)
    context_summary: str = Field(
        default="",
        max_length=200,
        description="Brief statistical count of detected issues.",
    )
