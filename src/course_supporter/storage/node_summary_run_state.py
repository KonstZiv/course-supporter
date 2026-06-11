"""Run-state schema for ``node_summary_regeneration`` jobs (vision §3 KD10).

KD10 line 1026 puts per-run + per-node statuses on the **pipeline-run
entity** (the Job row), explicitly NOT on the CourseNode/Raw — otherwise
the freshness signal becomes a third ephemeral axis next to
``content_hash`` and ``enclosing_context_source_hash``. ``Job`` already
carries the canonical columns: ``current_stage`` (the active pass —
``bottomup`` / ``topdown`` / NULL when not running), and
``stage_progress`` (JSONB, schema intentionally not enforced at the DB
level per the orm comment).

This module pins the JSON shape under ``Job.stage_progress`` for the
``node_summary_regeneration`` job type and ships the round-trip
helpers (``to_jsonb`` / ``from_jsonb``) so the orchestrator and tests
read/write identical bytes. Schema decisions (Q-4 ratify, Phase 3.2.2):

* ``current_stage`` is **NOT duplicated** here — it lives in the
  ``Job.current_stage`` column and would drift if mirrored.
* Completion is signalled via ``Job.status='completed'``, never via a
  ``current_stage='completed'`` sentinel value.
* ``pass1`` / ``pass2`` are per-node maps from ``uuid`` to one of four
  statuses: ``pending`` / ``done`` / ``skipped_memo`` / ``error``.
* ``errors`` is an append-only list (resume sees the full history of
  attempts across reruns).
* ``scope`` records what ``validate_scope`` decided when the run
  started; ``uncovered_stale`` is informational and does NOT block
  the run from proceeding.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NodeSummaryNodeStatus(StrEnum):
    """Per-node lifecycle status inside a ``node_summary_regeneration`` run."""

    PENDING = "pending"
    """Node is in scope but the pass has not yet committed against it."""

    DONE = "done"
    """LLM hook executed and node was committed (canonical fields written)."""

    SKIPPED_MEMO = "skipped_memo"
    """Memoization short-circuit fired; LLM hook was NOT invoked."""

    ERROR = "error"
    """A raise inside the visit was caught; see ``errors[]`` for the entry."""


class NodeSummaryRunScope(BaseModel):
    """What scope-validation decided at run start (vision §3 KD10 scope-valid).

    ``uncovered_stale`` is informational only — the API at 3.2.4 will
    surface it as a 422 (with ``force=True`` bypass), but the
    orchestrator itself does NOT raise on uncovered stale (Q-1
    ratify: logic-only, HTTP-surfacing is 3.2.4).
    """

    model_config = ConfigDict(extra="forbid")

    in_scope_node_ids: list[uuid.UUID] = Field(default_factory=list)
    uncovered_stale_node_ids: list[uuid.UUID] = Field(default_factory=list)


class NodeSummaryRunError(BaseModel):
    """A single error observed during a per-node visit (append-only)."""

    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    stage: str
    """``'bottomup'`` / ``'topdown'`` (matches Job.current_stage values)."""
    reason: str
    """Short operator-facing diagnostic; full stack trace stays in logs."""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NodeSummaryRunState(BaseModel):
    """Top-level shape persisted under ``Job.stage_progress``.

    ``vertex_node_id`` + ``force`` are the run inputs (echoed back so
    resume sees the original parameters without re-deriving them from
    ``Job.input_params``). ``scope`` is the scope-validation result.
    ``pass1`` / ``pass2`` are per-node status maps keyed by
    ``CourseNode.id``. ``errors`` is append-only.

    JSON round-trip serializes UUIDs and datetimes via Pydantic's
    default JSON-mode dump so the resulting dict is JSONB-storable
    without further massaging on the SQLAlchemy side.
    """

    model_config = ConfigDict(extra="forbid")

    vertex_node_id: uuid.UUID
    force: bool = False
    scope: NodeSummaryRunScope = Field(default_factory=NodeSummaryRunScope)
    pass1: dict[uuid.UUID, NodeSummaryNodeStatus] = Field(default_factory=dict)
    pass2: dict[uuid.UUID, NodeSummaryNodeStatus] = Field(default_factory=dict)
    errors: list[NodeSummaryRunError] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_jsonb(self) -> dict[str, Any]:
        """Serialize to a JSONB-compatible dict (UUID/datetime → str)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_jsonb(cls, payload: dict[str, Any]) -> NodeSummaryRunState:
        """Deserialize from ``Job.stage_progress`` JSONB content."""
        return cls.model_validate(payload)
