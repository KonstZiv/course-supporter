"""Tests for the deterministic review scoring (sprint-mentor T6).

These pin the determinism boundary (decision 2): aggregation, denoise clamping, verdict
derivation and the D9 ledger are pure arithmetic over the LLM judgments — no LLM
is consulted for a number.
"""

from __future__ import annotations

import pytest

from course_supporter.homework.review_config import (
    LayerWeights,
    VerdictThresholds,
)
from course_supporter.homework.review_scoring import (
    aggregate_score,
    apply_denoise,
    build_score_signals,
    derive_verdict,
)
from course_supporter.models.mentor_review import Layer

_WEIGHTS = LayerWeights(node=0.5, course=0.3, industry=0.2)
_THRESHOLDS = VerdictThresholds(pass_score=60, correct_min=85, partially_correct_min=50)


def _layer(name: str, score: int) -> Layer:
    weight = {"node": 0.5, "course": 0.3, "industry": 0.2}[name]
    return Layer(
        layer=name,  # type: ignore[arg-type]
        weight=weight,
        score=score,
        strengths=[],
        weaknesses=[],
    )


class TestAggregate:
    def test_weighted_sum(self) -> None:
        layers = [_layer("node", 80), _layer("course", 70), _layer("industry", 60)]
        # 0.5*80 + 0.3*70 + 0.2*60 = 40 + 21 + 12 = 73
        assert aggregate_score(layers, _WEIGHTS) == 73

    def test_all_equal_scores_returns_that_score(self) -> None:
        layers = [_layer("node", 50), _layer("course", 50), _layer("industry", 50)]
        assert aggregate_score(layers, _WEIGHTS) == 50

    def test_result_clamped_to_100(self) -> None:
        layers = [_layer("node", 100), _layer("course", 100), _layer("industry", 100)]
        assert aggregate_score(layers, _WEIGHTS) == 100


class TestDenoise:
    def test_positive_delta_within_cap(self) -> None:
        assert apply_denoise(70, 5, cap=10) == 75

    def test_negative_delta_within_cap(self) -> None:
        assert apply_denoise(70, -5, cap=10) == 65

    def test_delta_exceeding_cap_is_clamped(self) -> None:
        assert apply_denoise(70, 50, cap=10) == 80
        assert apply_denoise(70, -50, cap=10) == 60

    def test_result_clamped_to_range(self) -> None:
        assert apply_denoise(95, 10, cap=10) == 100
        assert apply_denoise(3, -10, cap=10) == 0

    def test_zero_delta_is_identity(self) -> None:
        assert apply_denoise(70, 0, cap=10) == 70


class TestVerdict:
    @pytest.mark.parametrize(
        ("score", "passed", "correctness"),
        [
            (90, True, "correct"),
            (85, True, "correct"),
            (70, True, "partially_correct"),
            (60, True, "partially_correct"),
            (55, False, "partially_correct"),
            (50, False, "partially_correct"),
            (40, False, "incorrect"),
            (0, False, "incorrect"),
        ],
    )
    def test_bands(self, score: int, passed: bool, correctness: str) -> None:
        verdict = derive_verdict(score, _THRESHOLDS)
        assert verdict.passed is passed
        assert verdict.correctness == correctness


class TestScoreSignals:
    def test_layer_above_neutral_is_up_signal(self) -> None:
        layers = [_layer("node", 80), _layer("course", 50), _layer("industry", 50)]
        signals = build_score_signals(
            layers,
            layer_rationales={"node": "strong design"},
            denoise_delta=0,
            history_reason="",
        )
        # node: 0.5 * (80-50) = 15 up. course/industry at neutral -> no signal.
        assert len(signals) == 1
        assert signals[0].layer == "node"
        assert signals[0].direction == "up"
        assert signals[0].magnitude == 15
        assert signals[0].reason == "strong design"

    def test_layer_below_neutral_is_down_signal(self) -> None:
        layers = [_layer("node", 30), _layer("course", 50), _layer("industry", 50)]
        signals = build_score_signals(
            layers, layer_rationales={}, denoise_delta=0, history_reason=""
        )
        assert len(signals) == 1
        assert signals[0].direction == "down"
        assert signals[0].magnitude == 10  # 0.5 * |30-50| = 10

    def test_neutral_layers_produce_no_signal(self) -> None:
        layers = [_layer("node", 50), _layer("course", 50), _layer("industry", 50)]
        signals = build_score_signals(
            layers, layer_rationales={}, denoise_delta=0, history_reason=""
        )
        assert signals == []

    def test_history_delta_adds_history_signal(self) -> None:
        layers = [_layer("node", 50), _layer("course", 50), _layer("industry", 50)]
        signals = build_score_signals(
            layers,
            layer_rationales={},
            denoise_delta=-4,
            history_reason="repeated the off-by-one mistake",
        )
        assert len(signals) == 1
        assert signals[0].layer == "history"
        assert signals[0].direction == "down"
        assert signals[0].magnitude == 4
        assert signals[0].reason == "repeated the off-by-one mistake"
