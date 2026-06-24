"""The stored sanity-gate contract (sprint-mentor T7, KD15 §1300).

The shape persisted to ``HomeworkSubmission.sanity_result`` and emitted by the
``sanity_check`` stage — they are the same shape (unlike the review graph, the
sanity gate does no code-derivation between the LLM output and what is stored).
Lives in ``models/`` (not ``agents/``) so the pipeline and webhook can read it
without depending on the agent layer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SanityVerdict = Literal["match", "mismatch"]


class SanityClassification(BaseModel):
    """The sanity gate's verdict on one submission.

    ``verdict`` is the binary judgement; ``confidence`` is the model's own
    certainty in ``[0, 1]`` (the gate is terminal only when a ``mismatch``
    clears the configured confidence floor — ratified A3-conservative);
    ``reason`` is a short human explanation carried into the mismatch webhook.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: SanityVerdict
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model's certainty in the verdict, 0.0-1.0.",
    )
    reason: str = Field(description="Short explanation of the verdict.")
