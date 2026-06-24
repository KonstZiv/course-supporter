"""The ``review_result`` contract written by the Mentor review graph (T6).

Vision §1369-1400 ("Mentor review як граф"): the graph produces a structured
trace of its three phases — per-layer judgments (D8), the weighted aggregate,
the history reconciliation (D10), the D9 score-signal ledger, and the
caller-facing verdict. This module is the canonical Pydantic shape of that
JSONB blob; the graph assembles and validates a :class:`ReviewResult` before
persisting ``submission.review_result``.

The model is the place several ratified invariants become *type-level* gates:

* **D8 — exactly three active layers.** ``layers`` must hold the node, course
  and industry layers, each present exactly once.
* **D7 x D8 — bounded weights.** Every layer weight is strictly in ``(0, 1)``;
  no layer is disabled or dominant.
* **D10 — all scores preserved.** Per-layer scores, the aggregate, and the
  denoised score all live here; nothing is discarded.

The ``verdict`` block is the only key pinned in T3 (read by
``build_reviewed_payload`` for the webhook). T6 must keep its shape unchanged;
its values are CODE-derived from the denoised score, not LLM-emitted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LayerName = Literal["node", "course", "industry"]
"""The three always-active criteria layers (D8), by increasing scope."""

SignalLayer = Literal["node", "course", "industry", "history"]
"""Origin of a score signal — a criteria layer, or the history denoise (D10)."""

Correctness = Literal["correct", "partially_correct", "incorrect"]
"""Coarse correctness verdict (T3/T5 webhook contract — do not change)."""

Direction = Literal["up", "down"]
"""Whether a score signal raised or lowered the score (D9)."""


class Layer(BaseModel):
    """One layer's signed judgment (D8): rubric application or autonomous review."""

    model_config = ConfigDict(extra="forbid")

    layer: LayerName
    weight: float = Field(
        gt=0.0,
        lt=1.0,
        description="Aggregation weight, strictly in (0, 1) per D7 x D8.",
    )
    score: int = Field(ge=0, le=100, description="This layer's 0-100 score.")
    strengths: list[str] = Field(description="What the submission did well here.")
    weaknesses: list[str] = Field(description="Where it fell short on this layer.")


class ReconciliationItem(BaseModel):
    """One history match (D10): a repeated or corrected mistake vs a prior attempt."""

    model_config = ConfigDict(extra="forbid")

    signal: str = Field(description="Short label for the recurring/corrected point.")
    prior_submission_id: str = Field(
        description="The earlier submission this match refers to."
    )
    note: str = Field(description="Why it matters — explain deeper / acknowledge.")


class HistoryReconciliation(BaseModel):
    """Phase 3 (D10): recidivism + corrections vs history, and the bounded delta."""

    model_config = ConfigDict(extra="forbid")

    recidivism: list[ReconciliationItem] = Field(
        description="Mistakes repeated from earlier attempts (explain deeper)."
    )
    corrections: list[ReconciliationItem] = Field(
        description="Previously-flagged mistakes now fixed (acknowledge progress)."
    )
    denoise_delta: int = Field(
        description="Signed nudge applied to the aggregate (bounded by config cap)."
    )
    denoised_score: int = Field(
        ge=0,
        le=100,
        description="Final post-denoise score — mirrors the typed score column.",
    )


class ScoreSignal(BaseModel):
    """One entry in the D9 ledger: something that moved the score, with a trace.

    Code-derived from the layer scores (weight x deviation) and the history
    delta, so the ledger provably reflects the real score arithmetic. The
    synthesis step is fed these verbatim and must surface each one in the
    review text (D9 — everything that moved the score leaves a trace).
    """

    model_config = ConfigDict(extra="forbid")

    layer: SignalLayer
    direction: Direction
    magnitude: int = Field(ge=0, description="Absolute points the signal moved.")
    reason: str = Field(description="Human-readable cause (LLM rationale / note).")


class Verdict(BaseModel):
    """Caller-facing verdict (T3/T5 contract). CODE-derived from the final score."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    correctness: Correctness


class ReviewResult(BaseModel):
    """The full ``review_result`` JSONB the Mentor graph persists (T6)."""

    model_config = ConfigDict(extra="forbid")

    layers: list[Layer] = Field(description="Exactly three: node, course, industry.")
    aggregate_score: int = Field(
        ge=0,
        le=100,
        description="Weighted aggregate of the layer scores, pre-denoise (code).",
    )
    history_reconciliation: HistoryReconciliation
    score_signals: list[ScoreSignal] = Field(
        description="D9 ledger — every score mover, surfaced in review_markdown."
    )
    verdict: Verdict

    @model_validator(mode="after")
    def _exactly_three_distinct_layers(self) -> ReviewResult:
        """D8: the node, course and industry layers, each present exactly once."""
        names = [layer.layer for layer in self.layers]
        if sorted(names) != ["course", "industry", "node"]:
            msg = (
                "review_result.layers must contain exactly the three D8 layers "
                f"(node, course, industry), each once; got {names}."
            )
            raise ValueError(msg)
        return self
