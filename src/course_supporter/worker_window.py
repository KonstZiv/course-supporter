"""Work window service for heavy task scheduling.

Determines whether heavy tasks (whisper, vision, OCR) are allowed to run
based on a configurable time window with timezone support.
"""

from __future__ import annotations

import zoneinfo
from dataclasses import dataclass
from datetime import datetime, time, timedelta


@dataclass(frozen=True)
class WorkWindow:
    """Time window during which heavy tasks are allowed to execute.

    When ``enabled=False``, the window is effectively 24/7 —
    :meth:`is_active_now` always returns ``True``.

    Supports overnight windows (e.g. start=22:00, end=06:00).
    """

    start: time
    end: time
    tz: zoneinfo.ZoneInfo
    enabled: bool

    @classmethod
    def from_settings(
        cls,
        start: time,
        end: time,
        tz_name: str,
        enabled: bool,
    ) -> WorkWindow:
        """Create from Settings fields."""
        return cls(
            start=start,
            end=end,
            tz=zoneinfo.ZoneInfo(tz_name),
            enabled=enabled,
        )

    @property
    def is_overnight(self) -> bool:
        """Window spans midnight (e.g. 22:00 → 06:00)."""
        return self.start > self.end

    def _now(self) -> datetime:
        """Current time in the configured timezone."""
        return datetime.now(self.tz)

    def is_active_now(self) -> bool:
        """Check if the current time falls within the work window."""
        if not self.enabled:
            return True

        current_time = self._now().time()

        if self.is_overnight:
            # e.g. 22:00 → 06:00: active if after start OR before end
            return current_time >= self.start or current_time < self.end
        # e.g. 02:00 → 06:30: active if between start and end
        return self.start <= current_time < self.end

    def next_start(self) -> datetime:
        """Calculate when the window next opens.

        If currently inside the window, returns the *next* opening
        (i.e. tomorrow's start for regular windows, or today/tomorrow
        depending on overnight position).

        If the window is disabled, returns current time (always active).
        """
        if not self.enabled:
            return self._now()

        now = self._now()
        today_start = now.replace(
            hour=self.start.hour,
            minute=self.start.minute,
            second=0,
            microsecond=0,
        )

        if now < today_start:
            return today_start
        # Today's start has passed — next opening is tomorrow
        return today_start + timedelta(days=1)

    def remaining_today(self) -> timedelta:
        """Time remaining until the window closes.

        Returns ``timedelta(0)`` if outside the window or disabled
        with no meaningful remaining (returns large delta for disabled).
        """
        if not self.enabled:
            # 24/7 mode — conceptually infinite remaining
            return timedelta(hours=24)

        if not self.is_active_now():
            return timedelta(0)

        now = self._now()
        today_end = now.replace(
            hour=self.end.hour,
            minute=self.end.minute,
            second=0,
            microsecond=0,
        )

        if self.is_overnight and now.time() >= self.start:
            # We're in the first part (after start, before midnight)
            # End is tomorrow
            today_end += timedelta(days=1)
        elif self.is_overnight and now.time() < self.end:
            # We're in the second part (after midnight, before end)
            # End is today — already correct
            pass

        return max(today_end - now, timedelta(0))
