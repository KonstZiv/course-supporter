"""Invariant tests for the review_result Pydantic contract (sprint-mentor T6).

The contract is where D8 / D7 x D8 / D10 become type-level gates: exactly three
distinct layers, each weight strictly in (0, 1), all scores 0..100. These tests
lock those gates so a malformed graph output cannot be persisted.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from course_supporter.models.mentor_review import (
    HistoryReconciliation,
    Layer,
    ReviewResult,
    ScoreSignal,
    Verdict,
)


def _layer(name: str, weight: float, score: int) -> Layer:
    return Layer(
        layer=name,  # type: ignore[arg-type]
        weight=weight,
        score=score,
        strengths=["ok"],
        weaknesses=[],
    )


def _history() -> HistoryReconciliation:
    return HistoryReconciliation(
        recidivism=[], corrections=[], denoise_delta=0, denoised_score=70
    )


def _valid_result(layers: list[Layer]) -> ReviewResult:
    return ReviewResult(
        layers=layers,
        aggregate_score=70,
        history_reconciliation=_history(),
        score_signals=[],
        verdict=Verdict(passed=True, correctness="partially_correct"),
    )


class TestLayerInvariants:
    def test_weight_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _layer("node", 0.0, 70)

    def test_weight_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _layer("node", 1.0, 70)

    def test_weight_strictly_inside_unit_interval_accepted(self) -> None:
        assert _layer("node", 0.5, 70).weight == 0.5

    @pytest.mark.parametrize("score", [-1, 101])
    def test_score_out_of_range_rejected(self, score: int) -> None:
        with pytest.raises(ValidationError):
            _layer("node", 0.5, score)

    def test_unknown_layer_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Layer(
                layer="galaxy",  # type: ignore[arg-type]
                weight=0.5,
                score=70,
                strengths=[],
                weaknesses=[],
            )


class TestExactlyThreeLayers:
    def test_three_distinct_layers_accepted(self) -> None:
        result = _valid_result(
            [
                _layer("node", 0.5, 80),
                _layer("course", 0.3, 70),
                _layer("industry", 0.2, 60),
            ]
        )
        assert {layer.layer for layer in result.layers} == {
            "node",
            "course",
            "industry",
        }

    def test_missing_layer_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _valid_result([_layer("node", 0.5, 80), _layer("course", 0.5, 70)])

    def test_duplicate_layer_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _valid_result(
                [
                    _layer("node", 0.4, 80),
                    _layer("node", 0.4, 70),
                    _layer("industry", 0.2, 60),
                ]
            )

    def test_extra_fourth_layer_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _valid_result(
                [
                    _layer("node", 0.3, 80),
                    _layer("course", 0.3, 70),
                    _layer("industry", 0.2, 60),
                    _layer("node", 0.2, 50),
                ]
            )


class TestStructuralStrictness:
    def test_extra_keys_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ScoreSignal(
                layer="node",  # type: ignore[arg-type]
                direction="up",  # type: ignore[arg-type]
                magnitude=4,
                reason="x",
                bogus="nope",  # type: ignore[call-arg]
            )

    def test_negative_magnitude_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScoreSignal(
                layer="node",  # type: ignore[arg-type]
                direction="down",  # type: ignore[arg-type]
                magnitude=-1,
                reason="x",
            )

    def test_denoised_score_range_enforced(self) -> None:
        with pytest.raises(ValidationError):
            HistoryReconciliation(
                recidivism=[], corrections=[], denoise_delta=0, denoised_score=101
            )
