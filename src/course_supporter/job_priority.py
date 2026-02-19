"""Job priority system with work window enforcement.

Heavy tasks (whisper, vision, OCR) use NORMAL priority and respect the
work window. Light tasks (fingerprint, LLM calls) use IMMEDIATE priority
and run at any time.
"""

from __future__ import annotations

from enum import StrEnum

import structlog
from arq import Retry

from course_supporter.config import get_settings
from course_supporter.worker_window import WorkWindow


class JobPriority(StrEnum):
    """Task execution priority."""

    IMMEDIATE = "immediate"
    NORMAL = "normal"


def get_work_window() -> WorkWindow:
    """Build a WorkWindow from current settings."""
    s = get_settings()
    return WorkWindow.from_settings(
        start=s.worker_heavy_window_start,
        end=s.worker_heavy_window_end,
        tz_name=s.worker_heavy_window_tz,
        enabled=s.worker_heavy_window_enabled,
    )


def check_work_window(priority: JobPriority) -> None:
    """Raise ``arq.Retry`` if a NORMAL job is outside the work window.

    IMMEDIATE jobs and disabled windows pass through without checks.

    Raises:
        arq.Retry: with ``defer`` set to the next window opening.
    """
    if priority == JobPriority.IMMEDIATE:
        return

    window = get_work_window()
    if window.is_active_now():
        return

    next_start = window.next_start()
    defer_seconds = (next_start - window._now()).total_seconds()
    log = structlog.get_logger()
    log.info(
        "job_deferred_to_window",
        priority=priority,
        next_window_start=next_start.isoformat(),
        defer_seconds=int(defer_seconds),
    )
    raise Retry(defer=defer_seconds)
