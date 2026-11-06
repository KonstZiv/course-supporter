"""The schedule of a periodic task: five cron fields, read into ARQ's terms.

A schedule is written as in crontab — ``minute hour day month weekday``, UTC —
so an operator can set it from the environment (``PERIODIC_<TASK>_CRON``).
Each field takes ``*``, a number, a range ``a-b``, a step ``*/n`` or ``a-b/n``,
and comma lists of those. A weekday is cron's: 0 or 7 is Sunday.

ARQ's ``cron()`` takes sets of values per field, and two of its rules differ
from crontab's, so :func:`parse_cron` translates them:

* ARQ's weekday is Python's (Monday = 0), not cron's (Sunday = 0);
* ARQ demands that BOTH the day and the weekday match, where crontab runs on
  EITHER. A schedule that restricts both is refused rather than read wrongly.
"""

from __future__ import annotations

from dataclasses import dataclass

_FIELDS = ("minute", "hour", "day", "month", "weekday")
_BOUNDS = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day": (1, 31),
    "month": (1, 12),
    "weekday": (0, 7),
}


@dataclass(frozen=True, slots=True)
class CronSchedule:
    """The values of each field at which a task runs; None is every value."""

    minute: frozenset[int] | None
    hour: frozenset[int] | None
    day: frozenset[int] | None
    month: frozenset[int] | None
    weekday: frozenset[int] | None
    """In ARQ's terms: Monday = 0 … Sunday = 6."""


def _number(text: str, field: str) -> int:
    low, high = _BOUNDS[field]
    if not text.isdigit() or not low <= int(text) <= high:
        raise ValueError(f"{field}: {text!r} is not a number in {low}-{high}")
    return int(text)


def _part(text: str, field: str) -> set[int]:
    """One comma-separated part: ``*``, ``n``, ``a-b``, ``*/s`` or ``a-b/s``."""
    span, _, step_text = text.partition("/")
    step = _number(step_text, "minute") if step_text else 1
    if step == 0:
        raise ValueError(f"{field}: a step of 0 in {text!r}")
    low, high = _BOUNDS[field]
    if span == "*":
        start, end = low, high
    elif "-" in span:
        first, _, last = span.partition("-")
        start, end = _number(first, field), _number(last, field)
        if start > end:
            raise ValueError(f"{field}: the range {span!r} runs backwards")
    else:
        start = _number(span, field)
        end = high if step_text else start
    return set(range(start, end + 1, step))


def _field(text: str, field: str) -> frozenset[int] | None:
    if text == "*":
        return None
    values: set[int] = set()
    for part in text.split(","):
        values |= _part(part, field)
    if field == "weekday":
        # cron: Sunday is 0 or 7, Monday 1; ARQ (Python): Monday 0, Sunday 6.
        values = {(value - 1) % 7 for value in values}
    return frozenset(values)


def parse_cron(text: str) -> CronSchedule:
    """Read five cron fields (module docstring).

    Raises:
        ValueError: not five fields, a value out of range, or both the day and
            the weekday restricted.
    """
    parts = text.split()
    if len(parts) != len(_FIELDS):
        raise ValueError(
            f"a schedule is five fields (minute hour day month weekday): {text!r}"
        )
    fields = {
        name: _field(part, name) for name, part in zip(_FIELDS, parts, strict=True)
    }
    if fields["day"] is not None and fields["weekday"] is not None:
        raise ValueError(
            f"restrict the day or the weekday, not both: {text!r} (ARQ would "
            "need both to match, crontab either)"
        )
    return CronSchedule(**fields)
