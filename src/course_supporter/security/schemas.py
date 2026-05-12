"""Pydantic schemas + audit data carriers for the security layer.

Stage 2 LLM safety classifier (vision §KD14, §KD15):

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

Submission extraction + audit context (Phase 2.1 C2 migration per
KD-2.1-I + KD-2.1-J + implementer discretion within KD-2.1-I body
authorization):

* :class:`FileContent`, :class:`SubmissionContent` -- Pydantic models
  for archive-extraction output (formerly in ``models/safety.py``).
* :class:`CourseContext` -- Pydantic model for Stage 2 prompt input
  (formerly in ``models/safety.py``).
* :class:`SecurityContext` -- frozen dataclass for audit metadata
  attached to :class:`SecurityRejectedError` via ``.enrich()``
  (formerly in ``safety/exceptions.py``).
* :class:`SecurityWarning` -- mutable dataclass for non-fatal security
  observations collected during extraction (formerly in
  ``safety/exceptions.py``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

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


# ── Submission extraction + audit context ─────────────────────────
#
# Migrated 2026-05-11 (Phase 2.1 C2 per KD-2.1-H/I/J).
# - FileContent / SubmissionContent / CourseContext: from
#   models/safety.py (Pydantic models per KD-2.1-J).
# - SecurityContext / SecurityWarning: from safety/exceptions.py
#   (@dataclass data carriers per KD-2.1-I body authorization +
#   implementer discretion: SecurityWarning is consumed by
#   SubmissionContent.security_warnings field, so co-locating
#   avoids backward canonical→legacy import dependency).


@dataclass(frozen=True)
class SecurityContext:
    """Audit context attached to security violations.

    Migrated from ``safety/exceptions.py`` per Phase 2.1 C2 (KD-2.1-I
    body authorization). Frozen dataclass for immutability — instances
    flow through audit log pipelines and shouldn't mutate post-
    construction.

    Used by :meth:`SecurityRejectedError.enrich` to attach caller-side
    context (tenant, student, submission) after library-side raise.
    Mirrors legacy ``safety.exceptions.SecurityContext`` signature
    exactly — no field renames, no Pydantic conversion — to preserve
    drop-in compatibility for consumer at ``api/tasks.py:1416``.
    """

    tenant_id: uuid.UUID | None = None
    student_id: uuid.UUID | None = None
    submission_id: uuid.UUID | None = None
    file_url: str | None = None
    filename: str | None = None

    def as_log_dict(self) -> dict[str, Any]:
        """Return non-None fields as a dict suitable for structlog binding."""
        return {k: str(v) for k, v in self.__dict__.items() if v is not None}


@dataclass
class SecurityWarning:
    """Non-fatal security observation collected during extraction.

    Migrated from ``safety/exceptions.py`` per Phase 2.1 C2 (implementer
    discretion within KD-2.1-I scope — see module docstring). Mutable
    dataclass — populated incrementally by
    :func:`course_supporter.security.archive.extract_submission_content`
    as it processes archive entries and emits per-entry observations
    (symlinks skipped, path traversal sanitized).
    """

    violation_type: str
    message: str
    filename: str | None = None
    raw_filename: str | None = None

    def as_log_dict(self) -> dict[str, Any]:
        """Structured payload for log emission."""
        return {k: v for k, v in self.__dict__.items() if v is not None}


class FileContent(BaseModel):
    """Content of a single file extracted from a submission.

    Migrated from ``models/safety.py`` per Phase 2.1 C2 (KD-2.1-J).
    """

    filename: str = Field(description="Filename or path within archive.")
    content: str = Field(description="Text content of the file.")
    size: int = Field(description="Size in bytes of the raw content.")


class SubmissionContent(BaseModel):
    """Aggregated content extracted from a homework submission.

    Migrated from ``models/safety.py`` per Phase 2.1 C2 (KD-2.1-J).
    ``security_warnings`` field is now typed as
    ``list[SecurityWarning]`` (was ``list[Any]`` in legacy location) —
    type-narrowing improvement enabled by SecurityWarning canonical
    relocation.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    files: list[FileContent] = Field(description="Individual file contents.")
    total_size: int = Field(description="Total size in bytes across all files.")
    security_warnings: list[SecurityWarning] = Field(
        default_factory=list,
        description="Non-fatal security observations found during extraction.",
        exclude=True,
    )

    @property
    def full_text(self) -> str:
        """Concatenate all file contents for analysis."""
        parts: list[str] = []
        for f in self.files:
            parts.append(f"--- {f.filename} ---")
            parts.append(f.content)
        return "\n".join(parts)


class CourseContext(BaseModel):
    """Minimal course context for relevance checking.

    Migrated from ``models/safety.py`` per Phase 2.1 C2 (KD-2.1-J).
    Consumed by :func:`run_stage2_safety_check` to populate optional
    Jinja vars in ``prompts/safety_check/v1.md`` per KD-1.2-I (course
    context preservation).
    """

    course_title: str = Field(description="Title of the course (root node).")
    course_description: str = Field(
        default="", description="Description of the course."
    )
    node_title: str = Field(description="Title of the target node.")
    node_description: str = Field(
        default="", description="Description of the target node."
    )
    outline_summary: str = Field(
        default="",
        description="Outline summary from AuthoredDocument, if available.",
    )
