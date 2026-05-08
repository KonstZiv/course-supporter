"""Pydantic schemas for the Stage 2 LLM safety classifier (vision §KD14, §KD15).

* :class:`ViolationCategory` -- enum of LLM-detected violation
  types. Values land verbatim in
  ``HomeworkSubmission.safety_result: JSONB`` per vision §KD15
  (forward-compat for Phase 4.1).

* :class:`SafetyResult` -- structured output of the classifier,
  parsed from the LLM JSON response. ``extra="forbid"`` follows
  the 0.5 precedent for input-side strictness; an LLM hallucinating
  an unknown key triggers ``SafetyValidationError`` upstream.

* :class:`Stage1RejectionResult` -- synthetic outcome for Stage 1
  rejections (no LLM call). Persists in the same JSONB column as
  :class:`SafetyResult` so downstream readers have a single column
  to query; the ``source`` literal field is the tagged-union
  discriminator distinguishing Stage 1 rejections from Stage 2
  verdicts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from course_supporter.security.exceptions import ErrorCategory


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

    source: Literal["stage2"] = "stage2"
    is_safe: bool
    violations: list[ViolationCategory]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class Stage1RejectionResult(BaseModel):
    """Synthetic safety result for Stage 1 rejections.

    Stage 1 (synchronous validation: extension whitelist, MIME
    magic, size cap, charset, regex prompt-injection, unicode,
    archive structure) raises :class:`SecurityRejectedError` before
    any LLM call. To preserve a uniform
    ``HomeworkSubmission.safety_result`` JSONB column shape — same
    column persists either Stage 1 rejection or Stage 2 verdict —
    the rejection is captured into this synthetic Pydantic model.

    The ``source`` field is the tagged-union discriminator
    distinguishing Stage 1 rejections (``"stage1"``) from Stage 2
    verdicts (:class:`SafetyResult`, ``"stage2"``). Readers can
    dispatch on ``source`` or use Pydantic discriminated unions if
    historical data needs strict typing.

    ``is_safe`` is fixed ``False`` (Stage 1 only emits this shape on
    rejection); ``category`` is the :class:`ErrorCategory` enum
    value from the underlying ``SecurityRejectedError``; ``detail``
    is the human-readable detail string from the same exception.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["stage1"] = "stage1"
    is_safe: Literal[False] = False
    category: ErrorCategory
    detail: str
