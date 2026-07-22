"""Unit tests for the L3 ``processing_phase`` derivation (root cause #2).

Locks the FULL status matrix (every ``JOB_TRANSITIONS`` key) and guards against
a seventh Job status slipping in without a phase decision — the failure mode
that let the dead ``running`` token survive for years inside a duplicated set.
"""

import pytest

from course_supporter.storage.job_repository import (
    AT_REST_STATUSES,
    IN_FLIGHT_STATUSES,
    JOB_TRANSITIONS,
)
from course_supporter.storage.processing_phase import (
    derive_processing_phase,
    is_awaiting_author,
)

_EXPECTED_STATUSES = {
    "queued",
    "active",
    "complete",
    "failed",
    "cancelled",
    "obsolete",
}


class TestDeriveProcessingPhase:
    """Deterministic precedence (Рат.3): error > in-flight split > ready."""

    def test_queued_job_is_queued(self) -> None:
        assert derive_processing_phase(None, "queued") == "queued"

    def test_active_job_is_processing(self) -> None:
        assert derive_processing_phase(None, "active") == "processing"

    def test_no_job_is_ready(self) -> None:
        assert derive_processing_phase(None, None) == "ready"

    @pytest.mark.parametrize("terminal", sorted(AT_REST_STATUSES))
    def test_terminal_job_is_ready(self, terminal: str) -> None:
        # A stale terminal ``job_id`` on a live document is an anomaly and is
        # treated as "no job" — never a lying in-flight phase (Рат.3).
        assert derive_processing_phase(None, terminal) == "ready"

    @pytest.mark.parametrize("status", [*sorted(_EXPECTED_STATUSES), None])
    def test_error_dominates_every_status(self, status: str | None) -> None:
        assert derive_processing_phase("boom", status) == "error"

    def test_empty_error_string_is_not_error(self) -> None:
        # "" is falsy → not an error (mirrors the ``state`` property semantics).
        assert derive_processing_phase("", "queued") == "queued"

    def test_terminal_values_mirror_state_verbatim(self) -> None:
        # ready/error tokens equal what ``state`` would report; only the
        # in-flight ``pending`` case is refined into queued/processing.
        assert derive_processing_phase(None, None) == "ready"
        assert derive_processing_phase("x", None) == "error"


class TestPhaseMatrixTotality:
    """A seventh Job status must force an explicit phase decision."""

    def test_status_set_is_frozen(self) -> None:
        # If a status is added to JOB_TRANSITIONS this fails loudly, forcing
        # the author to decide its processing_phase (impl-rules#13 — all dict
        # elements, not a subset).
        assert set(JOB_TRANSITIONS) == _EXPECTED_STATUSES

    @pytest.mark.parametrize("status", sorted(JOB_TRANSITIONS))
    def test_every_status_maps_by_category(self, status: str) -> None:
        # in-flight → a visible in-flight phase; at-rest → ready (mirrors
        # state). A new in-flight status would default to 'ready' here and
        # trip this assertion — the loud guard the contract asks for.
        phase = derive_processing_phase(None, status)
        if status in IN_FLIGHT_STATUSES:
            assert phase in {"queued", "processing"}
        else:
            assert phase == "ready"

    def test_never_empty(self) -> None:
        for status in [*list(JOB_TRANSITIONS), None]:
            for err in (None, "", "boom"):
                assert derive_processing_phase(err, status)


class TestDeriveWithAwaitingAuthor:
    """№21: awaiting_author sits between the in-flight split and ready."""

    def test_awaiting_no_job_is_awaiting_author(self) -> None:
        assert (
            derive_processing_phase(None, None, awaiting_author=True)
            == "awaiting_author"
        )

    def test_awaiting_outranks_ready(self) -> None:
        # No job + awaiting signal → awaiting_author, NOT ready — the
        # retry-with-live-old-summary case must show the confirm screen.
        assert derive_processing_phase(None, None, awaiting_author=True) != "ready"

    @pytest.mark.parametrize("terminal", sorted(AT_REST_STATUSES))
    def test_awaiting_outranks_stale_terminal(self, terminal: str) -> None:
        # A stale terminal job_id is "no job"; the awaiting signal still wins.
        assert (
            derive_processing_phase(None, terminal, awaiting_author=True)
            == "awaiting_author"
        )

    @pytest.mark.parametrize("status", sorted(IN_FLIGHT_STATUSES))
    def test_in_flight_job_outranks_awaiting(self, status: str) -> None:
        # A running prep/processing shows queued/processing, not awaiting_author.
        assert derive_processing_phase(None, status, awaiting_author=True) in {
            "queued",
            "processing",
        }

    def test_error_outranks_awaiting(self) -> None:
        assert derive_processing_phase("boom", None, awaiting_author=True) == "error"

    def test_not_awaiting_no_job_is_ready(self) -> None:
        # D2-rollback shape: a covered proposal yields awaiting=False → ready
        # (recovery via re-retry, ratified 2026-07-22).
        assert derive_processing_phase(None, None, awaiting_author=False) == "ready"


_PROPOSAL = {"tree_digest": "d1", "files": {}}


class TestIsAwaitingAuthor:
    """№21: the file_roles signal — a proposal is present AND not covered."""

    def test_none_file_roles(self) -> None:
        assert is_awaiting_author(None) is False

    def test_empty_file_roles(self) -> None:
        assert is_awaiting_author({}) is False

    def test_proposal_absent(self) -> None:
        assert is_awaiting_author({"decision": {"tree_digest": "d1"}}) is False

    def test_proposal_without_decision_awaits(self) -> None:
        assert is_awaiting_author({"proposal": _PROPOSAL}) is True

    def test_decision_covers_proposal_does_not_await(self) -> None:
        assert (
            is_awaiting_author(
                {"proposal": _PROPOSAL, "decision": {"tree_digest": "d1"}}
            )
            is False
        )

    def test_stale_decision_awaits(self) -> None:
        # Tree changed under a saved decision → digest mismatch → awaits again.
        assert (
            is_awaiting_author(
                {"proposal": _PROPOSAL, "decision": {"tree_digest": "OLD"}}
            )
            is True
        )
