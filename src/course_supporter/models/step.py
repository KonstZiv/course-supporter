"""Data contracts for multi-pass generation pipeline.

Defines immutable input/output types for generation steps.
Each step receives a StepInput and produces a StepOutput,
regardless of step type (generate, reconcile, refine).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from typing import Any

    from course_supporter.llm.schemas import LLMResponse
    from course_supporter.models.course import (
        CourseNodeSummary,
        CourseStructure,
        SlideTimecodeRef,
    )
    from course_supporter.models.source import SourceDocument


class StepType(StrEnum):
    """Type of generation step."""

    GENERATE = "generate"
    RECONCILE = "reconcile"
    REFINE = "refine"


@dataclass(frozen=True)
class NodeSummary:
    """Compact representation of a node's generation result.

    Used for cross-node context in the sliding window:
    parent summary, sibling summaries, children summaries.
    """

    node_id: uuid.UUID
    title: str
    summary: str
    core_concepts: list[str]
    mentioned_concepts: list[str]
    structure_snapshot_id: uuid.UUID | None


@dataclass(frozen=True)
class ChildSnapshotContext:
    """Full snapshot of a child node for parent context.

    Provides the complete generated structure from a child node,
    used by parent nodes instead of raw materials to reduce token count.
    Includes summary_nested_nodes for recursive compression of deeper levels.
    """

    node_id: uuid.UUID
    title: str
    structure: dict[str, Any]
    summary: str
    core_concepts: list[str]
    mentioned_concepts: list[str]
    summary_nested_nodes: str


class CorrectionAction(StrEnum):
    """Allowed correction actions for reconciliation."""

    RENAME = "rename"
    ADD = "add"
    REMOVE = "remove"
    MOVE = "move"


@dataclass(frozen=True)
class Correction:
    """A single correction suggested by reconciliation."""

    target_node_id: uuid.UUID
    field: str
    action: CorrectionAction
    old_value: str | None
    new_value: str | None
    reason: str


@dataclass(frozen=True)
class StepInput:
    """Immutable input for a single generation/reconciliation step.

    Assembled by Step Executor from DB data before calling an Agent.
    The Agent receives this and has no access to DB or job state.
    """

    node_id: uuid.UUID
    step_type: StepType

    # Raw data (from MaterialEntries)
    materials: list[SourceDocument]

    # Context from other nodes (sliding window)
    children_summaries: list[NodeSummary]
    parent_context: NodeSummary | None
    sibling_summaries: list[NodeSummary]

    # Existing structure JSON from serialize_tree_for_guided() — internal only
    existing_structure: str | None

    # Generation parameters
    mode: Literal["free", "guided"]
    material_tree: list[CourseNodeSummary]

    # Full child snapshots for parent context (replaces raw child materials)
    children_snapshots: list[ChildSnapshotContext] = field(default_factory=list)
    slide_timecode_refs: list[SlideTimecodeRef] = field(default_factory=list)

    # MaterialOutline JSON (preferred over raw materials when available)
    outline_context: str | None = None


@dataclass(frozen=True)
class StepOutput:
    """Immutable output from a single generation/reconciliation step.

    Returned by an Agent to Step Executor for persistence.
    """

    structure: CourseStructure
    summary: str
    core_concepts: list[str]
    mentioned_concepts: list[str]

    # LLM metadata
    prompt_version: str
    response: LLMResponse

    # Compressed summary of child snapshots for recursive context
    summary_nested_nodes: str = ""

    # Reconciliation-specific (None for generate steps)
    corrections: list[Correction] | None = field(default=None)
    terminology_map: dict[str, str] | None = field(default=None)
