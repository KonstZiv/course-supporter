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


class TestRunErrorMachineReadableFields:
    """Phase 3.2.4 c3 — ``severity`` + ``error_class`` round-trip."""

    def test_explicit_warning_a2_round_trips(self) -> None:
        nid = uuid.uuid4()
        err = NodeSummaryRunError(
            node_id=nid,
            stage="topdown",
            reason="A2 (out-of-scope parent without Raw — uncovered_stale "
            "bypassed by force=True)",
            severity="WARNING",
            error_class="parent_intentionally_out_of_run_scope",
        )
        state = NodeSummaryRunState(vertex_node_id=uuid.uuid4(), errors=[err])
        restored = NodeSummaryRunState.from_jsonb(state.to_jsonb())
        assert len(restored.errors) == 1
        assert restored.errors[0].severity == "WARNING"
        assert restored.errors[0].error_class == "parent_intentionally_out_of_run_scope"

    def test_explicit_error_a1_round_trips(self) -> None:
        err = NodeSummaryRunError(
            node_id=uuid.uuid4(),
            stage="topdown",
            reason="A1 (in-scope parent without Raw — Pass 1 ordering violation)",
            severity="ERROR",
            error_class="parent_summary_missing_within_scope",
        )
        state = NodeSummaryRunState(vertex_node_id=uuid.uuid4(), errors=[err])
        restored = NodeSummaryRunState.from_jsonb(state.to_jsonb())
        assert restored.errors[0].severity == "ERROR"
        assert restored.errors[0].error_class == "parent_summary_missing_within_scope"

    def test_explicit_error_b_round_trips(self) -> None:
        err = NodeSummaryRunError(
            node_id=uuid.uuid4(),
            stage="topdown",
            reason="B (missing parent CourseNode — soft-delete race or "
            "structural inconsistency)",
            severity="ERROR",
            error_class="parent_node_missing_from_database",
        )
        state = NodeSummaryRunState(vertex_node_id=uuid.uuid4(), errors=[err])
        restored = NodeSummaryRunState.from_jsonb(state.to_jsonb())
        assert restored.errors[0].severity == "ERROR"
        assert restored.errors[0].error_class == "parent_node_missing_from_database"

    def test_other_exception_path_yields_error_none(self) -> None:
        """All catch sites other than Pass 2 parent-missing emit
        ``(ERROR, None)`` — the constructor default for non-specialised
        failures."""
        err = NodeSummaryRunError(
            node_id=uuid.uuid4(),
            stage="bottomup",
            reason="LLM provider timeout",
            severity="ERROR",
            error_class=None,
        )
        state = NodeSummaryRunState(vertex_node_id=uuid.uuid4(), errors=[err])
        restored = NodeSummaryRunState.from_jsonb(state.to_jsonb())
        assert restored.errors[0].severity == "ERROR"
        assert restored.errors[0].error_class is None


class TestRunErrorBackwardTolerance:
    """Pre-3.2.4 records (no ``severity`` / ``error_class`` keys) parse
    cleanly with defaults — the JSONB persistence channel needs no
    migration (ratified pre-flight rule §3.2.4 c3)."""

    def test_old_shape_without_new_fields_defaults_to_error_none(self) -> None:
        nid = uuid.uuid4()
        old_payload = {
            "vertex_node_id": str(uuid.uuid4()),
            "force": False,
            "scope": {"in_scope_node_ids": [], "uncovered_stale_node_ids": []},
            "pass1": {},
            "pass2": {},
            "errors": [
                {
                    "node_id": str(nid),
                    "stage": "bottomup",
                    "reason": "historical-shape failure",
                    "at": "2026-06-01T12:00:00+00:00",
                    # NB: no severity, no error_class — pre-3.2.4 shape.
                }
            ],
            "started_at": "2026-06-01T12:00:00+00:00",
            "updated_at": "2026-06-01T12:00:00+00:00",
        }
        restored = NodeSummaryRunState.from_jsonb(old_payload)
        assert len(restored.errors) == 1
        # Defaults applied: severity=ERROR (safer assumption),
        # error_class=None (unspecialised).
        assert restored.errors[0].severity == "ERROR"
        assert restored.errors[0].error_class is None
        # Original fields preserved verbatim.
        assert restored.errors[0].node_id == nid
        assert restored.errors[0].stage == "bottomup"
        assert restored.errors[0].reason == "historical-shape failure"

    def test_forward_extra_field_in_error_does_not_raise(self) -> None:
        """``NodeSummaryRunError.model_config['extra']='allow'`` — a
        future-task addition on the error entry survives resume-replay."""
        payload = {
            "vertex_node_id": str(uuid.uuid4()),
            "scope": {"in_scope_node_ids": [], "uncovered_stale_node_ids": []},
            "pass1": {},
            "pass2": {},
            "errors": [
                {
                    "node_id": str(uuid.uuid4()),
                    "stage": "topdown",
                    "reason": "future-task added a new key",
                    "at": "2026-06-01T12:00:00+00:00",
                    "future_field_we_dont_understand": {"hello": "world"},
                }
            ],
            "started_at": "2026-06-01T12:00:00+00:00",
            "updated_at": "2026-06-01T12:00:00+00:00",
        }
        # Must not raise.
        restored = NodeSummaryRunState.from_jsonb(payload)
        assert len(restored.errors) == 1


class TestClassifyPass2ParentMissingHelper:
    """Pure-function mapping of the two-bool discriminator onto the
    public severity + error_class enum (Phase 3.2.4 c3)."""

    def test_branch_b_missing_parent_course_node(self) -> None:
        from course_supporter.storage.node_summary_orchestrator import (
            _classify_pass2_parent_missing,
        )

        severity, error_class = _classify_pass2_parent_missing(
            parent_present=False, parent_in_scope=False
        )
        assert (severity, error_class) == (
            "ERROR",
            "parent_node_missing_from_database",
        )
        # parent_in_scope is force-coerced False when parent absent;
        # the helper must reach the B branch even on a logically
        # nonsensical (False, True) combo, defensively.
        severity, error_class = _classify_pass2_parent_missing(
            parent_present=False, parent_in_scope=True
        )
        assert (severity, error_class) == (
            "ERROR",
            "parent_node_missing_from_database",
        )

    def test_branch_a1_in_scope_parent_without_raw(self) -> None:
        from course_supporter.storage.node_summary_orchestrator import (
            _classify_pass2_parent_missing,
        )

        severity, error_class = _classify_pass2_parent_missing(
            parent_present=True, parent_in_scope=True
        )
        assert (severity, error_class) == (
            "ERROR",
            "parent_summary_missing_within_scope",
        )

    def test_branch_a2_out_of_scope_parent_force_bypass(self) -> None:
        from course_supporter.storage.node_summary_orchestrator import (
            _classify_pass2_parent_missing,
        )

        severity, error_class = _classify_pass2_parent_missing(
            parent_present=True, parent_in_scope=False
        )
        assert (severity, error_class) == (
            "WARNING",
            "parent_intentionally_out_of_run_scope",
        )
