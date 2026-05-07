"""KD3 cascade soft-delete marker test helpers.

Shared regex + tolerance + assertion helper for the KD3 author-content
scrub marker contract (vision §3 KD3, Amendment 23 + 24). Consumers:

- ``tests/unit/test_cascade_delete_service.py::TestKD3MarkerFormat`` —
  unit-level scrub callable contract.
- ``tests/storage/test_cascade_invalidation.py`` — cascade-engine
  integration with real DB.

Update this module on contract changes; both consumer suites pick
up the new format atomically.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

MARKER_REGEX = (
    r"^інформація видалена автором "
    r"(\d{2})-(\d{2})-(\d{4}) "
    r"(\d{2}):(\d{2}):(\d{2})$"
)
TIMESTAMP_TOLERANCE_SECONDS = 5


def parse_marker_timestamp(marker: str) -> datetime:
    """Parse the embedded timestamp from a KD3 marker string."""
    match = re.match(MARKER_REGEX, marker)
    assert match is not None, f"Marker did not match contract: {marker!r}"
    day, month, year, hour, minute, second = match.groups()
    return datetime(
        int(year),
        int(month),
        int(day),
        int(hour),
        int(minute),
        int(second),
        tzinfo=UTC,
    )


def assert_marker_recent(value: str) -> None:
    """Assert ``value`` matches the KD3 marker contract with a recent
    timestamp (within ``TIMESTAMP_TOLERANCE_SECONDS`` of now).
    """
    parsed = parse_marker_timestamp(value)
    drift = abs((datetime.now(UTC) - parsed).total_seconds())
    assert drift <= TIMESTAMP_TOLERANCE_SECONDS, (
        f"Marker timestamp drift {drift}s exceeds tolerance "
        f"{TIMESTAMP_TOLERANCE_SECONDS}s"
    )
