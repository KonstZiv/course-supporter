"""What happened to one row of the external-call register (mentor-rebuild task 01).

Purpose:
    ``ExternalServiceCall.success`` answers one question only — did the
    transport return a response. Whether that response was usable, whether a
    ladder rung was skipped or given up on, was until now readable only by
    parsing ``error_message`` (DD-SP-AC). These enums are the queryable
    answers, one column each.

Interface:
    :class:`CallOutcome` — the row's result; written on every new row from the
    ladder router (attempts and traces), webhook delivery and speech-to-text.
    ``NULL`` on historical rows (never back-filled:
    a guess from ``success`` + ``error_message`` would be indistinguishable
    from a recorded fact) and on the per-review metrics row, which is neither
    a call nor a trace.
    :class:`SkipReason` — why a rung was skipped without a call; set only on
    ``CallOutcome.SKIPPED`` rows.

Extending:
    A new value is a new enum member AND a migration widening the matching
    ``CHECK`` constraint on ``external_service_calls`` — the database refuses
    a value it has not been told about, so the two cannot drift silently
    (``tests/integration/test_external_service_call_db.py`` writes every
    member). Task 02 adds ``SkipReason`` "rate limited" exactly this way.
"""

from __future__ import annotations

from enum import StrEnum


class CallOutcome(StrEnum):
    """Result of one register row.

    Rows of an actual provider call:

    * ``SUCCESS`` — a non-empty response that the stage accepted.
    * ``TRANSPORT_ERROR`` — the call raised an infrastructure error (timeout,
      429, 5xx, network); the router retries these on the same rung. Also the
      failure value for rows with no response body to judge (webhook
      delivery, speech-to-text).
    * ``PROVIDER_REFUSAL`` — the provider rejected the call in a way that is
      not retried (auth, bad request, content filter). Points at the prompt or
      the safety settings.
    * ``INPUT_OVERFLOW`` — the provider refused the input as too large for the
      model. Points at the input size, not at the prompt — kept apart from a
      refusal so the "path" level (task 02) can react without parsing text.
    * ``EMPTY_AT_OUTPUT_CEILING`` — the response came back empty and the
      provider reported the output ceiling as the finish reason: the
      allowance was spent (typically on reasoning) before any answer.
    * ``EMPTY`` — the response came back empty with no ceiling reported.
    * ``INVALID_CONTENT`` — a non-empty response the stage's validator
      rejected (off-schema, truncated JSON, ...).

    Trace rows, written without a call (``success`` is ``NULL``):

    * ``SKIPPED`` — the rung was not attempted; see :class:`SkipReason`.
    * ``ABANDONED`` — the router gave up on the rung and moved on. The row
      carries no reason of its own: the attempt row just before it does.
    """

    SUCCESS = "success"
    TRANSPORT_ERROR = "transport_error"
    PROVIDER_REFUSAL = "provider_refusal"
    INPUT_OVERFLOW = "input_overflow"
    EMPTY_AT_OUTPUT_CEILING = "empty_at_output_ceiling"
    EMPTY = "empty"
    INVALID_CONTENT = "invalid_content"
    SKIPPED = "skipped"
    ABANDONED = "abandoned"


class SkipReason(StrEnum):
    """Why a ladder rung was skipped without a call.

    The register answers "why skipped", not "by how much": the budget
    numbers stay in ``LadderExhaustedError`` and the logs.
    """

    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    PROVIDER_DISABLED = "provider_disabled"
    INPUT_BUDGET_EXCEEDED = "input_budget_exceeded"
