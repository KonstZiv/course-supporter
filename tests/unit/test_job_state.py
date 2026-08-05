"""Unit tests for the author-facing ``job_state`` axis (step A §3).

Locks the FULL ``Job.status`` → work-state token matrix (every ``JOB_TRANSITIONS``
key) and guards against a seventh Job status slipping in without a token
decision — the same failure mode ``test_processing_phase`` guards for the
material phase axis.
"""

import pytest

from course_supporter.storage.job_repository import JOB_TRANSITIONS
from course_supporter.storage.job_state import JobState, derive_job_state

_EXPECTED_STATUSES = {
    "queued",
    "active",
    "complete",
    "failed",
    "cancelled",
    "obsolete",
}

# The full §3 table, asserted verbatim so a silent remap trips a test.
_EXPECTED_TOKENS: dict[str, JobState] = {
    "queued": "queued",
    "active": "processing",
    "complete": "ready",
    "failed": "error",
    "cancelled": "cancelled",
    "obsolete": "obsolete",
}


class TestDeriveJobState:
    """Pure ``Job.status`` → token, matching the §3 table verbatim."""

    @pytest.mark.parametrize(("status", "token"), sorted(_EXPECTED_TOKENS.items()))
    def test_status_maps_to_token(self, status: str, token: JobState) -> None:
        assert derive_job_state(status) == token

    def test_live_tokens_match_phase_words(self) -> None:
        # queued/processing are the SAME tokens the material phase uses, so the
        # band and the material card read identically (decision P2).
        assert derive_job_state("queued") == "queued"
        assert derive_job_state("active") == "processing"

    def test_awaiting_author_is_not_a_job_state(self) -> None:
        # awaiting_author is material-only (decision P8, 2026-08-05); no
        # Job.status produces it.
        assert "awaiting_author" not in set(_EXPECTED_TOKENS.values())

    def test_unknown_status_raises(self) -> None:
        # Fail-loud mirror of validate_job_type — the DB CHECK makes this
        # unreachable for a persisted row, so a raise beats a silent default.
        with pytest.raises(ValueError, match=r"Unknown Job\.status"):
            derive_job_state("running")


class TestJobStateMatrixTotality:
    """A seventh Job status must force an explicit token decision."""

    def test_status_set_is_frozen(self) -> None:
        # If a status is added to JOB_TRANSITIONS this fails loudly, forcing the
        # author to decide its job_state token (impl-rules#13 — all keys, not a
        # subset). The import-time guard in job_state.py is the runtime twin.
        assert set(JOB_TRANSITIONS) == _EXPECTED_STATUSES

    @pytest.mark.parametrize("status", sorted(JOB_TRANSITIONS))
    def test_every_status_maps(self, status: str) -> None:
        # Every canonical status yields a non-empty token — a new status added
        # without a token would raise here (KeyError → ValueError).
        assert derive_job_state(status)
