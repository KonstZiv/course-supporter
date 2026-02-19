"""Queue position and time estimate service.

Calculates estimated start and completion times for queued jobs,
accounting for queue depth, historical average durations, and the
work window schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from course_supporter.worker_window import WorkWindow

# Fallback when no historical data is available yet.
DEFAULT_AVG_DURATION = timedelta(minutes=10)


@dataclass(frozen=True)
class QueueEstimate:
    """Estimated timing for a job in the queue."""

    position_in_queue: int
    estimated_start: datetime
    estimated_complete: datetime
    next_window_start: datetime | None
    queue_summary: str


def _advance_through_window(
    start: datetime,
    work: timedelta,
    window: WorkWindow,
) -> datetime:
    """Advance *work* time forward respecting the work window.

    If the window is disabled (24/7 mode), simply adds *work* to *start*.
    Otherwise, only counts time that falls inside open windows, jumping
    across closed periods as needed.
    """
    if not window.enabled:
        return start + work

    remaining = work
    cursor = start

    # Safety limit to prevent infinite loops on misconfiguration.
    max_iterations = 400
    for _ in range(max_iterations):
        if remaining <= timedelta(0):
            break

        # Jump to the next window opening if we're outside.
        cursor_time = cursor.time()

        if not _time_in_window(cursor_time, window):
            next_open = _next_window_open(cursor, window)
            cursor = next_open
            continue

        # We're inside the window — calculate how long until it closes.
        window_remaining = _time_until_close(cursor, window)

        if remaining <= window_remaining:
            cursor += remaining
            remaining = timedelta(0)
        else:
            # Consume this window and jump to next opening.
            remaining -= window_remaining
            cursor += window_remaining
            # Now cursor is at window close — next iteration will jump.

    return cursor


def _time_in_window(t: time, window: WorkWindow) -> bool:
    """Check if a plain time falls inside the window bounds."""
    if window.is_overnight:
        return t >= window.start or t < window.end
    return window.start <= t < window.end


def _next_window_open(cursor: datetime, window: WorkWindow) -> datetime:
    """Find the next window opening at or after *cursor*."""
    today_start = cursor.replace(
        hour=window.start.hour,
        minute=window.start.minute,
        second=0,
        microsecond=0,
    )
    if cursor < today_start:
        return today_start
    return today_start + timedelta(days=1)


def _time_until_close(cursor: datetime, window: WorkWindow) -> timedelta:
    """Time from *cursor* until the window closes."""
    close = cursor.replace(
        hour=window.end.hour,
        minute=window.end.minute,
        second=0,
        microsecond=0,
    )
    if window.is_overnight and cursor.time() >= window.start:
        # End is tomorrow.
        close += timedelta(days=1)
    # If close <= cursor (shouldn't happen inside window), clamp to 0.
    return max(close - cursor, timedelta(0))


def estimate_job(
    *,
    pending_count: int,
    avg_duration: timedelta | None,
    window: WorkWindow,
) -> QueueEstimate:
    """Build a queue estimate for the next submitted job.

    Args:
        pending_count: Number of jobs already queued (the new job will
            be at position ``pending_count + 1``).
        avg_duration: Average completion time from job history, or
            ``None`` if no data is available yet.
        window: The current work window configuration.

    Returns:
        A :class:`QueueEstimate` with timing predictions.
    """
    avg = avg_duration or DEFAULT_AVG_DURATION
    position = pending_count + 1
    queue_work = avg * pending_count

    now = window.now()

    # Determine the starting point for queue processing.
    next_window: datetime | None
    if window.enabled and not window.is_active_now():
        start_base = window.next_start()
        next_window = start_base
    else:
        start_base = now
        next_window = None if not window.enabled else window.next_start()

    # Walk through the queue work respecting the window.
    estimated_start = _advance_through_window(start_base, queue_work, window)
    estimated_complete = _advance_through_window(estimated_start, avg, window)

    # Build human-readable summary.
    if window.enabled:
        window_str = f"{window.start:%H:%M}-{window.end:%H:%M}"
        summary = (
            f"{pending_count} job(s) in queue, "
            f"~{_format_duration(avg)} per job, "
            f"window {window_str}"
        )
    else:
        summary = (
            f"{pending_count} job(s) in queue, "
            f"~{_format_duration(avg)} per job, "
            f"24/7 mode"
        )

    return QueueEstimate(
        position_in_queue=position,
        estimated_start=estimated_start,
        estimated_complete=estimated_complete,
        next_window_start=next_window if window.enabled else None,
        queue_summary=summary,
    )


def _format_duration(d: timedelta) -> str:
    """Format a timedelta as a human-readable string."""
    total_seconds = int(d.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if remaining_minutes:
        return f"{hours}h{remaining_minutes}m"
    return f"{hours}h"
