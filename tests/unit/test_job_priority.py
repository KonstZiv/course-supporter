"""Tests for job priority and work window enforcement."""

from datetime import UTC, datetime, time
from unittest.mock import patch

import pytest
from arq import Retry

from course_supporter.job_priority import (
    JobPriority,
    check_work_window,
    get_work_window,
)
from course_supporter.worker_window import WorkWindow


def _fixed_window(
    start: time,
    end: time,
    now: datetime,
    *,
    enabled: bool = True,
) -> WorkWindow:
    """Create a WorkWindow with a fixed now() for deterministic tests."""
    base = WorkWindow.from_settings(
        start=start,
        end=end,
        tz_name="UTC",
        enabled=enabled,
    )

    class Fixed(WorkWindow):
        def now(self) -> datetime:
            return now

    return Fixed(start=base.start, end=base.end, tz=base.tz, enabled=base.enabled)


class TestJobPriority:
    def test_values(self) -> None:
        assert JobPriority.IMMEDIATE == "immediate"
        assert JobPriority.NORMAL == "normal"

    def test_is_str(self) -> None:
        assert isinstance(JobPriority.IMMEDIATE, str)


class TestGetWorkWindow:
    def test_returns_work_window(self) -> None:
        w = get_work_window()
        assert isinstance(w, WorkWindow)

    def test_reads_from_settings(self) -> None:
        w = get_work_window()
        assert w.start == time(2, 0)
        assert w.end == time(6, 30)


class TestCheckWorkWindow:
    def test_immediate_always_passes(self) -> None:
        """IMMEDIATE jobs never raise Retry, even outside window."""
        window = _fixed_window(
            start=time(2, 0),
            end=time(6, 30),
            now=datetime(2026, 2, 19, 12, 0, tzinfo=UTC),
        )
        with patch(
            "course_supporter.job_priority.get_work_window",
            return_value=window,
        ):
            check_work_window(JobPriority.IMMEDIATE)

    def test_normal_inside_window_passes(self) -> None:
        """NORMAL jobs pass when window is active."""
        window = _fixed_window(
            start=time(2, 0),
            end=time(6, 30),
            now=datetime(2026, 2, 19, 3, 0, tzinfo=UTC),
        )
        with patch(
            "course_supporter.job_priority.get_work_window",
            return_value=window,
        ):
            check_work_window(JobPriority.NORMAL)

    def test_normal_outside_window_raises_retry(self) -> None:
        """NORMAL jobs raise Retry with defer when outside window."""
        window = _fixed_window(
            start=time(2, 0),
            end=time(6, 30),
            now=datetime(2026, 2, 19, 12, 0, tzinfo=UTC),
        )
        with (
            patch(
                "course_supporter.job_priority.get_work_window",
                return_value=window,
            ),
            pytest.raises(Retry) as exc_info,
        ):
            check_work_window(JobPriority.NORMAL)

        assert exc_info.value.defer_score is not None
        assert exc_info.value.defer_score > 0

    def test_normal_disabled_window_passes(self) -> None:
        """NORMAL jobs pass when window is disabled (24/7 mode)."""
        window = _fixed_window(
            start=time(2, 0),
            end=time(6, 30),
            now=datetime(2026, 2, 19, 12, 0, tzinfo=UTC),
            enabled=False,
        )
        with patch(
            "course_supporter.job_priority.get_work_window",
            return_value=window,
        ):
            check_work_window(JobPriority.NORMAL)
