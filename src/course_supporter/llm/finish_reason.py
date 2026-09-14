"""Provider-neutral reason a model stopped generating (mentor-rebuild task 01).

Purpose:
    Tell a response that ran out of its output ceiling apart from one that
    finished on its own. An empty body means very different things in the two
    cases: at the ceiling the model spent its whole allowance (typically on
    reasoning) and was cut off; without it the model chose to say nothing.
    The call register records the difference instead of a single "empty".

Interface:
    :class:`FinishReason` — the four values the rest of the system sees.
    :func:`normalize_finish_reason` — maps one vendor's raw value onto them.

Replacing / extending:
    Every connector owns its vendor's vocabulary and calls
    :func:`normalize_finish_reason` with the raw value and the vendor values
    that mean "ceiling" and "stop"; nothing outside the connector knows the
    dialect. A new connector adds its own vocabulary the same way. A vendor
    value not listed by its connector lands in ``OTHER``, never in ``STOP`` —
    an unrecognised reason must not read as a clean finish.

>>> normalize_finish_reason("length", ceiling={"length"}, stop={"stop"})
<FinishReason.OUTPUT_CEILING: 'output_ceiling'>
>>> normalize_finish_reason(None, ceiling={"length"}, stop={"stop"})
<FinishReason.UNKNOWN: 'unknown'>
>>> normalize_finish_reason("content_filter", ceiling={"length"}, stop={"stop"})
<FinishReason.OTHER: 'other'>
"""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum


class FinishReason(StrEnum):
    """Why generation stopped, normalised across providers.

    * ``OUTPUT_CEILING`` — the output token ceiling was reached.
    * ``STOP`` — the model finished on its own (end of turn / stop sequence).
    * ``OTHER`` — the provider named a different reason (safety filter,
      tool call, refusal, a value the connector does not list).
    * ``UNKNOWN`` — the provider gave no reason at all. Reserved for that
      case only, so "unknown" in the register always means "not reported".
    """

    OUTPUT_CEILING = "output_ceiling"
    STOP = "stop"
    OTHER = "other"
    UNKNOWN = "unknown"


def normalize_finish_reason(
    raw: object,
    *,
    ceiling: Collection[object],
    stop: Collection[object],
    unreported: Collection[object] = (),
) -> FinishReason:
    """Map a vendor's raw finish reason onto :class:`FinishReason`.

    Args:
        raw: The value the SDK exposes (string, SDK enum, or ``None``).
        ceiling: Vendor values meaning "output ceiling reached".
        stop: Vendor values meaning "finished on its own".
        unreported: Vendor placeholders that carry no reason (e.g. an
            ``UNSPECIFIED`` enum member); treated like ``None``.

    Returns:
        The normalised reason.
    """
    if raw is None or raw == "" or raw in unreported:
        return FinishReason.UNKNOWN
    if raw in ceiling:
        return FinishReason.OUTPUT_CEILING
    if raw in stop:
        return FinishReason.STOP
    return FinishReason.OTHER
