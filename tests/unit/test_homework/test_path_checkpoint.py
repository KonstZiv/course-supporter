"""The shape of a submission's checkpoint and its reconciliation rule (task 03).

Pure tests: the round trip and :meth:`PathCheckpoint.first_unfinished` are
decisions about a value, and the database adds nothing to them. What the
checkpoint does against real rows — durability and reading back from the
revision's latest job — is pinned in
``tests/integration/test_path_checkpoint_db.py``.
"""

from __future__ import annotations

import pytest

from course_supporter.homework.path_checkpoint import (
    FreezeReason,
    PathCheckpoint,
    StageState,
)
from course_supporter.homework.path_config import PathKey, SubmissionState
from course_supporter.models.source import AssignmentType

_KEY = PathKey(AssignmentType.TASK, SubmissionState.FIRST)


class TestShape:
    def test_started_marks_every_stage_pending(self) -> None:
        cp = PathCheckpoint.started(_KEY, ["safety", "attempt_classifier"])

        assert cp.stages == {
            "safety": StageState.PENDING,
            "attempt_classifier": StageState.PENDING,
        }
        assert cp.frozen_stage is None
        assert cp.frozen_reason is None
        assert cp.retries == 0

    def test_the_key_round_trips_as_the_registers_text(self) -> None:
        """The funds-port row stores ``str(path_key)``; the two must agree."""
        cp = PathCheckpoint.started(_KEY, [])

        assert cp.path_key == _KEY
        assert str(cp.path_key) == "task/first"

    def test_jsonb_round_trip_is_identical(self) -> None:
        cp = (
            PathCheckpoint.started(_KEY, ["safety", "attempt_classifier"])
            .with_stage_done("safety")
            .retried()
            .frozen("attempt_classifier", FreezeReason.STAGE_MONEY_CEILING)
        )

        back = PathCheckpoint.from_jsonb(cp.to_jsonb())

        assert back == cp
        assert back.to_jsonb() == cp.to_jsonb()

    def test_an_unknown_key_is_refused(self) -> None:
        """``extra='forbid'``: a typo must not be stored as a silent passenger."""
        payload = PathCheckpoint.started(_KEY, []).to_jsonb()
        payload["stagez"] = {}

        with pytest.raises(ValueError, match="stagez"):
            PathCheckpoint.from_jsonb(payload)

    def test_a_finished_stage_clears_the_freeze(self) -> None:
        """A stage that ran is the answer to why the run had stopped."""
        cp = (
            PathCheckpoint.started(_KEY, ["safety"])
            .frozen("safety", FreezeReason.STAGE_MONEY_CEILING)
            .with_stage_done("safety")
        )

        assert cp.frozen_stage is None
        assert cp.frozen_reason is None

    def test_retries_count_on_the_runs_own_ledger(self) -> None:
        cp = PathCheckpoint.started(_KEY, []).retried().retried()

        assert cp.retries == 2


class TestFirstUnfinished:
    def test_walks_in_order_and_stops_at_the_first_pending(self) -> None:
        cp = PathCheckpoint.started(_KEY, ["a", "b", "c"]).with_stage_done("a")

        assert cp.first_unfinished(["a", "b", "c"]) == "b"

    def test_nothing_left_answers_none(self) -> None:
        cp = (
            PathCheckpoint.started(_KEY, ["a", "b"])
            .with_stage_done("a")
            .with_stage_done("b")
        )

        assert cp.first_unfinished(["a", "b"]) is None

    def test_a_stage_the_checkpoint_does_not_know_is_unfinished(self) -> None:
        """Added to the configuration after the freeze — it has not run.

        This is the case the money-ceiling freeze is lifted by: the operator
        edits the file, and the edit may add a stage.
        """
        cp = PathCheckpoint.started(_KEY, ["a"]).with_stage_done("a")

        assert cp.first_unfinished(["a", "new"]) == "new"

    def test_a_state_for_a_stage_no_longer_listed_is_ignored(self) -> None:
        """Removed from the configuration — a run is not held up by it."""
        cp = PathCheckpoint.started(_KEY, ["a", "gone"]).with_stage_done("a")

        assert cp.first_unfinished(["a"]) is None

    def test_both_rules_at_once(self) -> None:
        """One stage removed, one added, one already done: only the new one runs."""
        cp = (
            PathCheckpoint.started(_KEY, ["a", "gone"])
            .with_stage_done("a")
            .with_stage_done("gone")
        )

        assert cp.first_unfinished(["a", "new"]) == "new"

    def test_the_order_is_the_lists_not_the_checkpoints(self) -> None:
        """The configuration decides the order; the record only says what ran."""
        cp = PathCheckpoint.started(_KEY, ["a", "b"])

        assert cp.first_unfinished(["b", "a"]) == "b"
