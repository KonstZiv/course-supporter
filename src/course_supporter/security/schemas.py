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

* :class:`CourseContext` -- Pydantic model for Stage 2 prompt input
  (formerly in ``models/safety.py``).
* :class:`SecurityContext` -- frozen dataclass for audit metadata
  attached to :class:`SecurityRejectedError` via ``.enrich()``
  (formerly in ``safety/exceptions.py``).

``FileContent`` / ``SubmissionContent`` / ``SecurityWarning`` lived here
too, as the output shape of the legacy submission extractor. KD-1.2-K kept
them while that transitional path existed; the path was deleted in DD-6-S,
and with it the only thing that ever built them.
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


class NotOpenedEntry(BaseModel):
    """One archive member the checker did not read, and why.

    A formatting problem in one file must not cost the student the whole
    submission, so an unreadable member is NAMED rather than fatal: the rest
    of the work is reviewed and this record says what was skipped. The
    student sees it on the submission detail; the Mentor sees the same list
    appended to the submission text, so a review cannot silently rest on a
    partial reading.

    ``reason`` is an :class:`ErrorCategory` rather than a free string so the
    read path renders it from the one code table the rest of the surface
    already uses, instead of growing a second vocabulary.

    Denylist-skipped members (``__MACOSX/`` and friends) are deliberately
    NOT recorded: they are packaging noise, not the student's content, and
    listing them would bury the entries that matter.
    """

    model_config = ConfigDict(extra="forbid")

    arcname: str
    reason: ErrorCategory
    size: int


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
    # Stage 1 fact carried on the Stage 2 verdict because this column is what
    # the read path reads: a passing submission must still be able to tell the
    # student which of their files were not read. Empty for every non-archive
    # submission and for archives read in full.
    not_opened: list[NotOpenedEntry] = Field(default_factory=list)

    # Carried onto the verdict the same way, and for the same reason: the
    # answer to "how was this file read" is decided in Stage 1 and needed
    # on the read path. ``"utf-8"`` when the bytes decoded directly (the
    # ordinary case); another name when recovery established and verified
    # one, and the review was done on that reading; ``None`` when the
    # question does not apply -- an archive recovers its members one by one
    # and a document arrives already decoded, so neither carries a single
    # answer here.
    recovered_encoding: str | None = None


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


# ── Audit context + Stage 2 input ─────────────────────────────────
#
# Migrated 2026-05-11 (Phase 2.1 C2 per KD-2.1-H/I/J).
# - CourseContext: from models/safety.py (Pydantic model per KD-2.1-J).
# - SecurityContext: from safety/exceptions.py (@dataclass data carrier
#   per KD-2.1-I body authorization).


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
