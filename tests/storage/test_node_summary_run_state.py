"""Pydantic round-trip tests for ``NodeSummaryRunState`` (Phase 3.2.2).

Pins the JSON shape that lives under ``Job.stage_progress`` for the
``node_summary_regeneration`` job type. ``Job.current_stage`` is the
column-of-truth for the active pass and is NOT mirrored into this
JSON (Q-4 ratify: avoid two sources for the same fact).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from course_supporter.storage.node_summary_run_state import (
    NodeSummaryNodeStatus,
    NodeSummaryRunError,
    NodeSummaryRunScope,
    NodeSummaryRunState,
)


class TestNodeSummaryRunStateRoundTrip:
    """JSON-mode round-trip — what we write into JSONB equals what we read back."""

    def test_minimal_roundtrip(self) -> None:
        vertex = uuid.uuid4()
        state = NodeSummaryRunState(vertex_node_id=vertex)
        payload = state.to_jsonb()
        restored = NodeSummaryRunState.from_jsonb(payload)
        assert restored.vertex_node_id == vertex
        assert restored.force is False
        assert restored.pass1 == {}
        assert restored.pass2 == {}
        assert restored.errors == []
        assert restored.scope.in_scope_node_ids == []
        assert restored.scope.uncovered_stale_node_ids == []

    def test_full_payload_roundtrip(self) -> None:
        vertex = uuid.uuid4()
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        state = NodeSummaryRunState(
            vertex_node_id=vertex,
            force=True,
            scope=NodeSummaryRunScope(
                in_scope_node_ids=[a, b, c],
                uncovered_stale_node_ids=[],
            ),
            pass1={
                a: NodeSummaryNodeStatus.DONE,
                b: NodeSummaryNodeStatus.SKIPPED_MEMO,
                c: NodeSummaryNodeStatus.PENDING,
            },
            pass2={a: NodeSummaryNodeStatus.PENDING},
            errors=[
                NodeSummaryRunError(
                    node_id=c,
                    stage="bottomup",
                    reason="LLM ladder exhausted",
                )
            ],
        )
        payload = state.to_jsonb()
        restored = NodeSummaryRunState.from_jsonb(payload)
        assert restored.force is True
        assert restored.scope.in_scope_node_ids == [a, b, c]
        assert restored.pass1[a] is NodeSummaryNodeStatus.DONE
        assert restored.pass1[b] is NodeSummaryNodeStatus.SKIPPED_MEMO
        assert restored.pass2[a] is NodeSummaryNodeStatus.PENDING
        assert len(restored.errors) == 1
        assert restored.errors[0].stage == "bottomup"
        assert restored.errors[0].node_id == c


class TestJsonbCompatibility:
    """JSON-mode dump produces JSONB-storable primitives (no UUID/datetime objects)."""

    def test_uuids_serialize_to_strings(self) -> None:
        state = NodeSummaryRunState(vertex_node_id=uuid.uuid4())
        payload = state.to_jsonb()
        assert isinstance(payload["vertex_node_id"], str)
        assert uuid.UUID(payload["vertex_node_id"]) == state.vertex_node_id

    def test_datetimes_serialize_to_iso8601(self) -> None:
        fixed = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
        state = NodeSummaryRunState(
            vertex_node_id=uuid.uuid4(),
            started_at=fixed,
            updated_at=fixed,
        )
        payload = state.to_jsonb()
        assert isinstance(payload["started_at"], str)
        assert payload["started_at"].startswith("2026-06-11T12:00:00")

    def test_status_enum_serializes_as_string_value(self) -> None:
        node_id = uuid.uuid4()
        state = NodeSummaryRunState(
            vertex_node_id=uuid.uuid4(),
            pass1={node_id: NodeSummaryNodeStatus.SKIPPED_MEMO},
        )
        payload = state.to_jsonb()
        # JSONB cannot key on enum; pydantic mode='json' stringifies
        # the UUID key and the StrEnum value.
        assert payload["pass1"][str(node_id)] == "skipped_memo"


class TestExtraForbidden:
    """``extra='forbid'`` pins the shape — unknown fields raise."""

    def test_unknown_top_level_field_raises(self) -> None:
        payload = NodeSummaryRunState(vertex_node_id=uuid.uuid4()).to_jsonb()
        payload["current_stage"] = "bottomup"  # Q-4 ratify: NOT allowed here
        import pytest

        with pytest.raises(ValueError):
            NodeSummaryRunState.from_jsonb(payload)
