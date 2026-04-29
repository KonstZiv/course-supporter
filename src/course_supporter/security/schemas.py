"""Pydantic schemas for the Stage 2 LLM safety classifier (vision §KD14, §KD15).

* :class:`ViolationCategory` -- enum of LLM-detected violation
  types. Values land verbatim in
  ``HomeworkSubmission.safety_result: JSONB`` per vision §KD15
  (forward-compat for Phase 4.1).

* :class:`SafetyResult` -- structured output of the classifier,
  parsed from the LLM JSON response. ``extra="forbid"`` follows
  the 0.5 precedent for input-side strictness; an LLM hallucinating
  an unknown key triggers ``SafetyValidationError`` upstream.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ViolationCategory(StrEnum):
    """LLM-detected violation taxonomy in Stage 2.

    Values land verbatim in ``HomeworkSubmission.safety_result``
    JSONB per vision §KD15. Forward-compat: adding new members is
    non-breaking; renaming or removing is breaking.
    """

    PROMPT_INJECTION = "prompt_injection"
    OFF_TOPIC = "off_topic"
    POLICY_VIOLATION = "policy_violation"
    SUSPICIOUS_BEHAVIOR = "suspicious_behavior"


class SafetyResult(BaseModel):
    """Stage 2 safety classifier output parsed from LLM JSON.

    Constructed via :meth:`pydantic.BaseModel.model_validate_json`
    against ``StageResult.content``. ``extra="forbid"`` catches LLM
    hallucination of unknown keys -- callers convert the resulting
    :class:`pydantic.ValidationError` into
    ``SafetyValidationError`` (terminal per 0.6 acceptance; see
    that exception's docstring for the deferred-retry rationale).

    The same JSON shape later persists into
    ``HomeworkSubmission.safety_result: JSONB`` per vision §KD15.

    Attributes:
        is_safe: Overall safety verdict. When ``False``,
            ``violations`` is expected to be non-empty (semantic
            invariant enforced by callers, not at schema level --
            an LLM violating it should not crash parsing).
        violations: List of detected violation categories.
        confidence: Classifier's self-reported confidence in
            ``[0.0, 1.0]``.
        reasoning: Short human-readable rationale for the verdict.
    """

    model_config = ConfigDict(extra="forbid")

    is_safe: bool
    violations: list[ViolationCategory]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
