"""The closed vocabularies of a register row's price (BIL-C).

Purpose:
    The call register is the source of every money figure the platform reads:
    the stage sum the funds port is told, the cost reports, margin telemetry.
    A figure that cannot say how it was obtained is a figure nobody can trust,
    so every row of a paid call names the kind of its sum and the tariff window
    its prices were taken from. The database refuses a word it has not been
    told about, and the storage layer builds those ``CHECK`` constraints from
    these enums — which is why they live here, below both ``llm/`` and
    ``storage/``.

Interface:
    :class:`CostKind` — how the row's ``cost_usd`` was obtained.
    :class:`TariffWindow` — which time-of-day price applied.

Extending:
    A new member is a new enum value AND a migration re-issuing the ``CHECK``
    built from that enum (Postgres cannot widen a CHECK in place), so Python and
    SQL cannot drift apart silently.
"""

from __future__ import annotations

from enum import StrEnum


class CostKind(StrEnum):
    """How a paid call's sum was obtained.

    * ``EXACT`` — every usage detail the provider reported, times the prices in
      force at the moment of the call (time of day included). Also the known
      zero of a failure the provider certainly does not bill: a 429 answer, or
      a connection that failed before the request was sent.
    * ``ESTIMATED`` — a number computed under a named approximation: input
      tokens counted by us for a failed call, cached tokens priced at the
      cache-miss rate because the catalog names no cached price, a price the
      catalog says is not yet in force, a prompt above the size the price holds
      for, speech-to-text without a billing granularity.
    * ``UNKNOWN`` — no defensible number: ``cost_usd`` stays NULL. Never a
      zero in its place.

    ``NULL`` (no kind) is the row of no paid call: a ladder trace, the
    per-review metrics row, the funds-port row, a webhook delivery.
    """

    EXACT = "exact"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class TariffWindow(StrEnum):
    """Which time-of-day price a row was priced at.

    * ``FLAT`` — the model has no time-of-day tariff.
    * ``PEAK`` / ``OFF_PEAK`` — the model's tariff, at the moment the attempt
      was sent (``call_started_at``).
    """

    FLAT = "flat"
    PEAK = "peak"
    OFF_PEAK = "off_peak"
