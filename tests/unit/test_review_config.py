"""Tests for the Mentor review config loader (sprint-mentor T6).

Locks the load-time invariants: weights strictly in (0, 1) and summing to 1.0
(the D7 x D8 invariant pinned at config level), verdict bands ordered, and the
shipped ``config/mentor_review.yaml`` actually loads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from course_supporter.homework.review_config import (
    LayerWeights,
    MentorReviewConfig,
    VerdictThresholds,
    load_mentor_review_config,
)

_SHIPPED = Path("config/mentor_review.yaml")


class TestShippedConfig:
    def test_shipped_config_loads(self) -> None:
        cfg = load_mentor_review_config(_SHIPPED)
        assert isinstance(cfg, MentorReviewConfig)
        # The defaults the YAML documents.
        assert cfg.layer_weights.node == 0.5
        assert cfg.denoise.cap == 10
        assert cfg.verdict_thresholds.pass_score == 60

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_mentor_review_config(Path("config/does_not_exist.yaml"))


class TestWeightInvariants:
    def test_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError, match=r"sum to 1\.0"):
            LayerWeights(node=0.5, course=0.5, industry=0.2)

    @pytest.mark.parametrize(
        ("node", "course", "industry"),
        [(0.0, 0.5, 0.5), (1.0, 0.0, 0.0), (0.8, 0.2, 0.0)],
    )
    def test_weight_outside_unit_interval_rejected(
        self, node: float, course: float, industry: float
    ) -> None:
        with pytest.raises(ValueError):
            LayerWeights(node=node, course=course, industry=industry)

    def test_valid_weights_accepted(self) -> None:
        weights = LayerWeights(node=0.4, course=0.4, industry=0.2)
        assert weights.node + weights.course + weights.industry == pytest.approx(1.0)


class TestVerdictBands:
    def test_bands_must_be_ordered(self) -> None:
        with pytest.raises(ValueError, match="partially_correct_min <= correct_min"):
            VerdictThresholds(pass_score=60, correct_min=50, partially_correct_min=80)

    def test_ordered_bands_accepted(self) -> None:
        bands = VerdictThresholds(
            pass_score=60, correct_min=85, partially_correct_min=50
        )
        assert bands.partially_correct_min <= bands.correct_min
