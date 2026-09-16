"""Error category taxonomy for the LLM ladder fallback policy (KD16).

The ``StageRouter`` classifies each provider exception into one of
four categories and applies a category-specific retry / fallback
policy. This module defines the enum and the router-level exception
types that drive that classification.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


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


class LadderStop(StrEnum):
    """Why a stage's ladder ended without a result — exactly one of three.

    A caller that has to react needs to tell them apart, and one closed
    vocabulary says so better than a set of flags: the ladder ends once, for one
    reason, and two flags could be true at the same time while nothing in the
    world could be.

    Reading the difference out of the last attempt's ``reason`` text would make
    a decision rest on a message.
    """

    EXHAUSTED = "exhausted"
    """Every rung was tried and every one failed. Worth retrying — a provider
    may be having a bad minute, and the next attempt meets a different one."""

    OUTPUT_CEILING = "output_ceiling"
    """A rung answered nothing with its output ceiling spent, and the walk
    stopped rather than buy a second empty answer (mentor-rebuild task 03).
    Not worth retrying: the next attempt meets the same ceiling with the same
    input. What has to change is the configuration."""

    MONEY_CEILING = "money_ceiling"
    """No rung could make a single attempt within what was left of the stage's
    money ceiling, so NOTHING was called. Not worth retrying for the same
    reason, and cheaper: nothing was spent finding out."""


class LadderExhaustedError(Exception):
    """Raised when a stage's ladder ended without a result.

    Attributes:
        stage_name: Stage name as declared in ``ladders_*.yaml``.
        attempts: ``(provider, model, reason)`` triples in the order
            they were attempted.
        stop: Which of the three endings this was (:class:`LadderStop`).
            ``EXHAUSTED`` unless a caller asked for one of the early stops.
    """

    _ENDING: ClassVar[dict[LadderStop, str]] = {
        LadderStop.EXHAUSTED: "exhausted",
        LadderStop.OUTPUT_CEILING: "stopped at the output ceiling",
        LadderStop.MONEY_CEILING: "stopped at the money ceiling",
    }

    def __init__(
        self,
        stage_name: str,
        attempts: list[tuple[str, str, str]],
        *,
        stop: LadderStop = LadderStop.EXHAUSTED,
    ) -> None:
        self.stage_name = stage_name
        self.attempts = attempts
        self.stop = stop
        details = "; ".join(f"{p}/{m}: {r}" for p, m, r in attempts)
        super().__init__(
            f"Ladder {self._ENDING[stop]} for stage '{stage_name}': {details}"
        )


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
