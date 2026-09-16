"""One answer to "what do we say about this attempt?" (mentor-rebuild task 03).

The server computes it once; the tree, the attempts list and the detail all
carry that same answer. These tests pin the mapping — every stored milestone,
and the reason code each state travels with.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from course_supporter.api.routes._portal_shared import (
    AWAITING_FUNDS_REASON_CODE,
    FAILED_REASON_CODE,
    curated_presentation,
)
from course_supporter.storage.orm import HomeworkStatus


def _submission(status: str, **attrs: Any) -> MagicMock:
    sub = MagicMock()
    sub.status = status
    sub.original_filename = "attempt.py"
    sub.safety_result = None
    for name, value in attrs.items():
        setattr(sub, name, value)
    return sub


class TestEveryStoredStatusHasExactlyOneState:
    @pytest.mark.parametrize("status", sorted(s.value for s in HomeworkStatus))
    def test_each_one_maps(self, status: str) -> None:
        """Totality: a milestone with nothing to say would be found by a student."""
        presentation = curated_presentation(_submission(status))

        assert presentation.state in {
            "not_opened",
            "not_an_attempt",
            "awaiting_funds",
            "in_progress",
            "reviewed",
        }

    def test_all_ten_statuses_are_covered(self) -> None:
        assert len(list(HomeworkStatus)) == 10

    @pytest.mark.parametrize(
        ("status", "state"),
        [
            ("rejected", "not_opened"),
            ("failed", "not_opened"),
            ("mismatch", "not_an_attempt"),
            ("awaiting_funds", "awaiting_funds"),
            ("received", "in_progress"),
            ("safety_ok", "in_progress"),
            ("sanity_ok", "in_progress"),
            ("reviewing", "in_progress"),
            ("completed", "reviewed"),
            ("delivered", "reviewed"),
        ],
    )
    def test_each_milestone_keeps_its_own_meaning(
        self, status: str, state: str
    ) -> None:
        """Named one by one, so a regrouping is a visible change."""
        assert curated_presentation(_submission(status)).state == state

    def test_the_four_in_flight_milestones_are_one_state(self) -> None:
        """Which gate has been passed is internal (language-rules)."""
        states = {
            curated_presentation(_submission(s)).state
            for s in ("received", "safety_ok", "sanity_ok", "reviewing")
        }

        assert states == {"in_progress"}


class TestReasonCodes:
    def test_mismatch_keeps_the_code_the_portal_already_knows(self) -> None:
        """The article keyed on it has to keep working."""
        assert curated_presentation(_submission("mismatch")).reason_code == "mismatch"

    def test_rejected_keeps_todays_curated_code(self) -> None:
        """The doors and the safety check already answer "why"."""
        stage2 = _submission(
            "rejected", safety_result={"source": "stage2", "is_safe": False}
        )

        assert curated_presentation(stage2).reason_code == "stage2_rejected"

    def test_failed_gets_its_own_code(self) -> None:
        """A run that broke told the student nothing before this task."""
        assert (
            curated_presentation(_submission("failed")).reason_code
            == FAILED_REASON_CODE
        )

    def test_awaiting_funds_gets_its_own_code(self) -> None:
        assert (
            curated_presentation(_submission("awaiting_funds")).reason_code
            == AWAITING_FUNDS_REASON_CODE
        )

    @pytest.mark.parametrize(
        "status", ["received", "safety_ok", "sanity_ok", "reviewing", "delivered"]
    )
    def test_no_code_where_the_state_says_everything(self, status: str) -> None:
        assert curated_presentation(_submission(status)).reason_code is None
