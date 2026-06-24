"""Deterministic scoring for the Mentor review graph (sprint-mentor T6).

The determinism boundary (vision review-mode protocol, ratified decision 2): numbers
the system can compute are NEVER asked of the LLM. The graph's LLM phases emit
*judgments* — per-layer scores + rationale (phase 1), recidivism/corrections +
a bounded delta (phase 3). Everything arithmetic lives here:

* :func:`aggregate_score` — the weighted aggregate over the per-layer scores,
  using the config weights (the LLM never sums).
* :func:`apply_denoise` — clamp the LLM delta to the config cap, then clamp the
  result to ``0..100``; the code owns the final number and its envelope.
* :func:`derive_verdict` — map the final score to ``passed`` + ``correctness``
  by config bands, guaranteeing score<->verdict consistency.
* :func:`build_score_signals` — assemble the D9 ledger from the layer scores
  (weight x deviation from neutral) and the history delta, so the ledger
  provably reflects the real score arithmetic; the ``reason`` text is the LLM's
  rationale / history note, never the magnitude or direction.

All functions are pure (no DB, no LLM) and unit-tested in isolation.
"""

from __future__ import annotations

from course_supporter.homework.review_config import (
    LayerWeights,
    VerdictThresholds,
)
from course_supporter.models.mentor_review import (
    Correctness,
    Layer,
    LayerName,
    ScoreSignal,
    Verdict,
)

# Reference point for D9-ledger magnitude/direction: a layer scoring above this
# pushed the aggregate up, below it pushed down. A derivation detail of the
# ledger (not a graph-behaviour knob), so it stays a module constant rather than
# a config field; lift to config if it ever needs author tuning.
_NEUTRAL_SCORE = 50


def _clamp_score(value: int) -> int:
    """Clamp a raw score to the valid 0..100 range."""
    return max(0, min(100, value))


def _weight_for(layer: LayerName, weights: LayerWeights) -> float:
    return {
        "node": weights.node,
        "course": weights.course,
        "industry": weights.industry,
    }[layer]


def aggregate_score(layers: list[Layer], weights: LayerWeights) -> int:
    """Weighted aggregate of the per-layer scores using the config weights.

    The LLM supplies the per-layer scores; the weights come from config
    (decision 4 — student preferences never touch them). The aggregate is
    rounded and clamped to ``0..100``.
    """
    total = sum(_weight_for(layer.layer, weights) * layer.score for layer in layers)
    return _clamp_score(round(total))


def apply_denoise(aggregate: int, delta: int, cap: int) -> int:
    """Apply the bounded history nudge (D10) to the aggregate.

    The delta is re-clamped to ``[-cap, +cap]`` defensively (the phase-3 agent
    is already asked to stay within the cap) so history nudges but never
    dominates the rubric; the result is clamped to ``0..100``.
    """
    bounded = max(-cap, min(cap, delta))
    return _clamp_score(aggregate + bounded)


def derive_verdict(denoised_score: int, thresholds: VerdictThresholds) -> Verdict:
    """Map the final (post-denoise) score to the caller-facing verdict.

    Code-derived (never LLM-emitted) so ``passed`` / ``correctness`` are always
    consistent with the headline ``score``.
    """
    if denoised_score >= thresholds.correct_min:
        correctness: Correctness = "correct"
    elif denoised_score >= thresholds.partially_correct_min:
        correctness = "partially_correct"
    else:
        correctness = "incorrect"
    return Verdict(
        passed=denoised_score >= thresholds.pass_score,
        correctness=correctness,
    )


def build_score_signals(
    layers: list[Layer],
    layer_rationales: dict[LayerName, str],
    denoise_delta: int,
    history_reason: str,
) -> list[ScoreSignal]:
    """Assemble the D9 ledger from the layer scores and the history delta.

    One signal per layer whose weighted deviation from neutral rounds to at
    least one point (a layer scoring exactly neutral did not move the score, so
    it gets no ledger entry — its weaknesses are still covered in the text by
    the structural D9 gate). One history signal when the denoise delta is
    non-zero. ``magnitude`` and ``direction`` are arithmetic (code); ``reason``
    is the LLM's rationale / history note. Each layer carries its config weight
    in ``Layer.weight`` (assigned by the orchestrator), so the deviation uses it
    directly.
    """
    signals: list[ScoreSignal] = []
    for layer in layers:
        contribution = layer.weight * (layer.score - _NEUTRAL_SCORE)
        magnitude = round(abs(contribution))
        if magnitude < 1:
            continue
        signals.append(
            ScoreSignal(
                layer=layer.layer,
                direction="up" if contribution > 0 else "down",
                magnitude=magnitude,
                reason=layer_rationales.get(layer.layer, ""),
            )
        )
    if denoise_delta != 0:
        signals.append(
            ScoreSignal(
                layer="history",
                direction="up" if denoise_delta > 0 else "down",
                magnitude=abs(denoise_delta),
                reason=history_reason,
            )
        )
    return signals
