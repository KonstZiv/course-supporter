"""Error category taxonomy for the LLM ladder fallback policy (KD16).

The ``StageRouter`` classifies each provider exception into one of
four categories and applies a category-specific retry / fallback
policy. This module defines the enum and the router-level exception
types that drive that classification.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    """Category of provider error driving ladder fallback policy.

    Per vision §3 KD16:

    * ``INFRASTRUCTURE`` -- transient (503, 429, timeout, network).
      Router retries the same model with exponential backoff before
      moving to the next ladder entry.
    * ``STRUCTURAL`` -- fixable parsing / schema error (malformed
      JSON, wrong enum value). Router does an instructor-style retry
      with error feedback before falling back.
    * ``SEMANTIC`` -- semantic failure, truncation, empty response,
      or any unrecognised exception. Router falls back to the next
      ladder entry immediately, without retry.
    * ``INPUT_OVERFLOW`` -- request does not fit the model's context
      window. Router falls back immediately; if every ladder entry
      is exhausted, raises ``LadderExhaustedError``.
    """

    INFRASTRUCTURE = "infrastructure"
    STRUCTURAL = "structural"
    SEMANTIC = "semantic"
    INPUT_OVERFLOW = "input_overflow"


class StructuralRetryError(Exception):
    """Raised by structured-output validators on malformed / off-schema output.

    The router catches this for an instructor-style retry: the
    original system + user prompts are preserved and ``feedback`` is
    appended as a user-role correction message. ``feedback`` must be
    short and actionable for the LLM (parser error summary).
    """

    def __init__(self, feedback: str) -> None:
        self.feedback = feedback
        super().__init__(feedback)


class LadderExhaustedError(Exception):
    """Raised when every ladder entry for a stage has been attempted and failed.

    Attributes:
        stage_name: Stage name as declared in ``ladders_*.yaml``.
        attempts: ``(provider, model, reason)`` triples in the order
            they were attempted.
    """

    def __init__(
        self,
        stage_name: str,
        attempts: list[tuple[str, str, str]],
    ) -> None:
        self.stage_name = stage_name
        self.attempts = attempts
        details = "; ".join(f"{p}/{m}: {r}" for p, m, r in attempts)
        super().__init__(f"Ladder exhausted for stage '{stage_name}': {details}")


class InvalidPromptError(Exception):
    """Raised by the markdown prompt loader on malformed prompt files.

    Surfaces user mistakes early -- e.g. content before any
    ``## RoleName`` header, unknown role name, or unparseable Jinja2
    template syntax.

    Attributes:
        prompt_ref: The ``prompt_ref`` value that failed to load.
        reason: Short human-readable explanation of the failure.
    """

    def __init__(self, prompt_ref: str, reason: str) -> None:
        self.prompt_ref = prompt_ref
        self.reason = reason
        super().__init__(f"Invalid prompt '{prompt_ref}': {reason}")
