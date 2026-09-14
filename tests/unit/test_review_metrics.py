"""Review metrics calculator and the metrics-row writer (mentor-rebuild task 01)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.review_metrics import (
    REVIEW_METRICS_ACTION,
    Claim,
    ReviewMetrics,
    ReviewMetricsCalculator,
    ShareMetricsCalculator,
)


def _claims(*flags: tuple[bool, bool]) -> list[Claim]:
    return [
        Claim(supported_by_reference=supported, covered_by_verdict=covered)
        for supported, covered in flags
    ]


class TestShareMetricsCalculator:
    @pytest.mark.parametrize(
        ("flags", "authenticity", "completeness"),
        [
            (((True, True), (True, False), (True, True), (False, False)), 0.75, 0.5),
            (((True, True),), 1.0, 1.0),
            (((False, False), (False, False)), 0.0, 0.0),
        ],
        ids=["mixed", "all-flagged", "none-flagged"],
    )
    def test_shares_of_flagged_claims(
        self,
        flags: tuple[tuple[bool, bool], ...],
        authenticity: float,
        completeness: float,
    ) -> None:
        metrics = ShareMetricsCalculator().calculate(_claims(*flags))

        assert metrics.authenticity == pytest.approx(authenticity)
        assert metrics.completeness == pytest.approx(completeness)

    def test_no_claims_means_no_metrics_not_zero(self) -> None:
        """Zero would claim "nothing was supported"; an empty review claimed nothing."""
        assert ShareMetricsCalculator().calculate([]) == ReviewMetrics(
            authenticity=None, completeness=None
        )

    def test_the_two_metrics_are_independent(self) -> None:
        metrics = ShareMetricsCalculator().calculate(
            _claims((True, False), (True, False))
        )
        assert (metrics.authenticity, metrics.completeness) == (1.0, 0.0)


class _EveryClaimCounts:
    """A replacement implementation, satisfying the port structurally."""

    def calculate(self, claims: Sequence[Claim]) -> ReviewMetrics:
        return ReviewMetrics(authenticity=1.0, completeness=float(len(claims)))


class TestReplaceableImplementation:
    def test_a_replacement_satisfies_the_port(self) -> None:
        calculator: ReviewMetricsCalculator = _EveryClaimCounts()
        assert calculator.calculate(_claims((False, False))).completeness == 1.0

    async def test_the_writer_stores_whatever_numbers_it_is_given(self) -> None:
        """The register write takes metrics, not a calculator: swapping the
        formula changes the numbers, never the write."""
        from course_supporter.service_logging import (
            _current_job_id,
            record_review_metrics,
        )

        metrics = _EveryClaimCounts().calculate(_claims((True, True), (False, True)))
        token = _current_job_id.set(uuid.uuid4())
        try:
            with patch(
                "course_supporter.service_logging._persist", new_callable=AsyncMock
            ) as persist:
                await record_review_metrics(MagicMock(), metrics)
        finally:
            _current_job_id.reset(token)

        kwargs = persist.await_args.kwargs
        assert kwargs["action"] == REVIEW_METRICS_ACTION
        assert (kwargs["authenticity"], kwargs["completeness"]) == (1.0, 2.0)
        # A metrics row records no call.
        assert (kwargs["provider"], kwargs["model_id"], kwargs["success"]) == (
            None,
            None,
            None,
        )
        assert kwargs.get("outcome") is None
