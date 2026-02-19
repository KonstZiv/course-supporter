"""Tests for job priority and work window enforcement."""

from datetime import time
from unittest.mock import patch

import pytest
from arq import Retry

from course_supporter.job_priority import (
    JobPriority,
    check_work_window,
    get_work_window,
)
from course_supporter.worker_window import WorkWindow


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
        window = WorkWindow.from_settings(
            start=time(2, 0),
            end=time(6, 30),
            tz_name="UTC",
            enabled=True,
        )
        with patch(
            "course_supporter.job_priority.get_work_window",
            return_value=window,
        ):
            # Should not raise regardless of current time
            check_work_window(JobPriority.IMMEDIATE)

    def test_normal_inside_window_passes(self) -> None:
        """NORMAL jobs pass when window is active."""
        window = WorkWindow.from_settings(
            start=time(0, 0),
            end=time(23, 59),
            tz_name="UTC",
            enabled=True,
        )
        with patch(
            "course_supporter.job_priority.get_work_window",
            return_value=window,
        ):
            check_work_window(JobPriority.NORMAL)

    def test_normal_outside_window_raises_retry(self) -> None:
        """NORMAL jobs raise Retry with defer when outside window."""
        window = WorkWindow.from_settings(
            start=time(2, 0),
            end=time(2, 1),
            tz_name="UTC",
            enabled=True,
        )
        with (
            patch(
                "course_supporter.job_priority.get_work_window",
                return_value=window,
            ),
            pytest.raises(Retry) as exc_info,
        ):
            check_work_window(JobPriority.NORMAL)

        # defer_score is defer in milliseconds (arq internal)
        assert exc_info.value.defer_score is not None
        assert exc_info.value.defer_score > 0

    def test_normal_disabled_window_passes(self) -> None:
        """NORMAL jobs pass when window is disabled (24/7 mode)."""
        window = WorkWindow.from_settings(
            start=time(2, 0),
            end=time(6, 30),
            tz_name="UTC",
            enabled=False,
        )
        with patch(
            "course_supporter.job_priority.get_work_window",
            return_value=window,
        ):
            check_work_window(JobPriority.NORMAL)
