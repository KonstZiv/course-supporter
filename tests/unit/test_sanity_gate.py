"""Unit tests for the sanity gate decision (sprint-mentor T7).

Covers the pure ``is_gated`` policy (verdict + confidence vs threshold). The
full ``SanityGateService.evaluate`` (which loads task context from the DB) is
exercised by ``tests/integration/test_homework_pipeline.py``.
"""

from __future__ import annotations

import pytest

from course_supporter.homework.sanity_gate import is_gated
from course_supporter.models.sanity import SanityClassification


def _c(verdict: str, confidence: float) -> SanityClassification:
    return SanityClassification(verdict=verdict, confidence=confidence, reason="x")


class TestIsGated:
    def test_high_confidence_mismatch_gates(self) -> None:
        assert is_gated(_c("mismatch", 0.95), 0.85) is True

    def test_confidence_exactly_at_threshold_gates(self) -> None:
        # Boundary: >= is inclusive — at-threshold confidence still gates.
        assert is_gated(_c("mismatch", 0.85), 0.85) is True

    def test_low_confidence_mismatch_passes_through(self) -> None:
        assert is_gated(_c("mismatch", 0.6), 0.85) is False

    @pytest.mark.parametrize("confidence", [0.0, 0.5, 0.85, 0.99, 1.0])
    def test_match_never_gates(self, confidence: float) -> None:
        # A match passes through at any confidence — only mismatch can gate.
        assert is_gated(_c("match", confidence), 0.85) is False
