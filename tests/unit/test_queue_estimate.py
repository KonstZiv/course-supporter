"""Tests for queue estimate service."""

from datetime import UTC, datetime, time, timedelta

import pytest

from course_supporter.queue_estimate import (
    DEFAULT_AVG_DURATION,
    QueueEstimate,
    estimate_job,
)
from course_supporter.worker_window import WorkWindow


def _window(
    start: time,
    end: time,
    now_dt: datetime,
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
            return now_dt

    return Fixed(start=base.start, end=base.end, tz=base.tz, enabled=base.enabled)


# ── Empty queue ──────────────────────────────────────────────


class TestEmptyQueue:
    def test_position_is_one(self) -> None:
        w = _window(time(2, 0), time(6, 30), datetime(2026, 2, 19, 3, 0, tzinfo=UTC))
        est = estimate_job(pending_count=0, avg_duration=None, window=w)
        assert est.position_in_queue == 1

    def test_start_is_now_inside_window(self) -> None:
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        w = _window(time(2, 0), time(6, 30), now)
        est = estimate_job(pending_count=0, avg_duration=None, window=w)
        assert est.estimated_start == now

    def test_complete_equals_start_plus_avg(self) -> None:
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        avg = timedelta(minutes=5)
        w = _window(time(2, 0), time(6, 30), now)
        est = estimate_job(pending_count=0, avg_duration=avg, window=w)
        assert est.estimated_complete == now + avg


# ── Queue with jobs ──────────────────────────────────────────


class TestQueueWithJobs:
    def test_five_jobs_in_queue(self) -> None:
        """Position is 6, start is after 5 x avg_duration."""
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        avg = timedelta(minutes=10)
        w = _window(time(2, 0), time(6, 30), now)
        est = estimate_job(pending_count=5, avg_duration=avg, window=w)
        assert est.position_in_queue == 6
        assert est.estimated_start == now + timedelta(minutes=50)
        assert est.estimated_complete == now + timedelta(minutes=60)

    def test_default_avg_used_when_none(self) -> None:
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        w = _window(time(2, 0), time(6, 30), now)
        est = estimate_job(pending_count=2, avg_duration=None, window=w)
        expected_start = now + DEFAULT_AVG_DURATION * 2
        assert est.estimated_start == expected_start
        assert est.estimated_complete == expected_start + DEFAULT_AVG_DURATION


# ── Outside window ───────────────────────────────────────────


class TestOutsideWindow:
    def test_start_deferred_to_next_window(self) -> None:
        """When outside window, start is next window opening + queue time."""
        now = datetime(2026, 2, 19, 12, 0, tzinfo=UTC)
        avg = timedelta(minutes=10)
        w = _window(time(2, 0), time(6, 30), now)
        est = estimate_job(pending_count=3, avg_duration=avg, window=w)

        next_window = datetime(2026, 2, 20, 2, 0, tzinfo=UTC)
        assert est.next_window_start == next_window
        assert est.estimated_start == next_window + timedelta(minutes=30)
        assert est.estimated_complete == next_window + timedelta(minutes=40)

    def test_next_window_start_populated(self) -> None:
        now = datetime(2026, 2, 19, 12, 0, tzinfo=UTC)
        w = _window(time(2, 0), time(6, 30), now)
        est = estimate_job(pending_count=0, avg_duration=None, window=w)
        assert est.next_window_start is not None


# ── Overflow across window boundary ─────────────────────────


class TestOverflow:
    def test_queue_overflows_to_next_day(self) -> None:
        """Queue too long for one window — spills into next day's window."""
        # Window 02:00-06:30 (4.5h = 270min). Now is 02:00.
        # 30 jobs x 10 min = 300 min needed. Only 270 min in this window.
        now = datetime(2026, 2, 19, 2, 0, tzinfo=UTC)
        avg = timedelta(minutes=10)
        w = _window(time(2, 0), time(6, 30), now)
        est = estimate_job(pending_count=30, avg_duration=avg, window=w)

        # 270 min consumed in first window (02:00-06:30).
        # 30 min remaining. Next window opens at 02:00 next day.
        next_day_start = datetime(2026, 2, 20, 2, 0, tzinfo=UTC)
        expected_start = next_day_start + timedelta(minutes=30)
        assert est.estimated_start == expected_start

    def test_overflow_multiple_days(self) -> None:
        """Very long queue overflows through multiple windows."""
        # Window 02:00-06:30 (270 min). Need 600 min of queue work.
        # Day 1: 270 min. Day 2: 270 min (540 total). Day 3: 60 min.
        now = datetime(2026, 2, 19, 2, 0, tzinfo=UTC)
        avg = timedelta(minutes=10)
        w = _window(time(2, 0), time(6, 30), now)
        est = estimate_job(pending_count=60, avg_duration=avg, window=w)

        # Day 1 consumes 270 min. Day 2 consumes 270 min (540 total).
        # 60 min remaining → Day 3 at 02:00 + 60min = 03:00.
        day3_start = datetime(2026, 2, 21, 2, 0, tzinfo=UTC)
        expected_start = day3_start + timedelta(minutes=60)
        assert est.estimated_start == expected_start


# ── 24/7 mode ────────────────────────────────────────────────


class TestDisabledWindow:
    def test_no_window_constraint(self) -> None:
        now = datetime(2026, 2, 19, 12, 0, tzinfo=UTC)
        avg = timedelta(minutes=10)
        w = _window(time(2, 0), time(6, 30), now, enabled=False)
        est = estimate_job(pending_count=5, avg_duration=avg, window=w)
        assert est.estimated_start == now + timedelta(minutes=50)
        assert est.estimated_complete == now + timedelta(minutes=60)

    def test_next_window_start_is_none(self) -> None:
        now = datetime(2026, 2, 19, 12, 0, tzinfo=UTC)
        w = _window(time(2, 0), time(6, 30), now, enabled=False)
        est = estimate_job(pending_count=0, avg_duration=None, window=w)
        assert est.next_window_start is None

    def test_summary_mentions_247(self) -> None:
        now = datetime(2026, 2, 19, 12, 0, tzinfo=UTC)
        w = _window(time(2, 0), time(6, 30), now, enabled=False)
        est = estimate_job(pending_count=0, avg_duration=None, window=w)
        assert "24/7" in est.queue_summary


# ── Overnight window ─────────────────────────────────────────


class TestOvernightWindow:
    def test_inside_overnight_first_half(self) -> None:
        """Inside overnight window before midnight."""
        now = datetime(2026, 2, 19, 23, 0, tzinfo=UTC)
        avg = timedelta(minutes=10)
        w = _window(time(22, 0), time(6, 0), now)
        est = estimate_job(pending_count=3, avg_duration=avg, window=w)
        assert est.estimated_start == now + timedelta(minutes=30)

    def test_inside_overnight_second_half(self) -> None:
        """Inside overnight window after midnight."""
        now = datetime(2026, 2, 20, 3, 0, tzinfo=UTC)
        avg = timedelta(minutes=10)
        w = _window(time(22, 0), time(6, 0), now)
        est = estimate_job(pending_count=3, avg_duration=avg, window=w)
        assert est.estimated_start == now + timedelta(minutes=30)

    def test_outside_overnight_window(self) -> None:
        """Outside overnight window during daytime."""
        now = datetime(2026, 2, 19, 12, 0, tzinfo=UTC)
        avg = timedelta(minutes=10)
        w = _window(time(22, 0), time(6, 0), now)
        est = estimate_job(pending_count=2, avg_duration=avg, window=w)

        next_open = datetime(2026, 2, 19, 22, 0, tzinfo=UTC)
        assert est.next_window_start == next_open
        assert est.estimated_start == next_open + timedelta(minutes=20)


# ── Summary format ───────────────────────────────────────────


class TestSummary:
    def test_summary_contains_count(self) -> None:
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        w = _window(time(2, 0), time(6, 30), now)
        avg = timedelta(minutes=10)
        est = estimate_job(pending_count=5, avg_duration=avg, window=w)
        assert "5 job(s)" in est.queue_summary

    def test_summary_contains_window_times(self) -> None:
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        w = _window(time(2, 0), time(6, 30), now)
        est = estimate_job(pending_count=0, avg_duration=None, window=w)
        assert "02:00-06:30" in est.queue_summary

    def test_summary_contains_duration(self) -> None:
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        w = _window(time(2, 0), time(6, 30), now)
        avg = timedelta(minutes=10)
        est = estimate_job(pending_count=0, avg_duration=avg, window=w)
        assert "10m" in est.queue_summary


# ── Dataclass ────────────────────────────────────────────────


class TestQueueEstimateDataclass:
    def test_is_frozen(self) -> None:
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        w = _window(time(2, 0), time(6, 30), now)
        est = estimate_job(pending_count=0, avg_duration=None, window=w)
        with pytest.raises(AttributeError):
            est.position_in_queue = 99  # type: ignore[misc]

    def test_fields_present(self) -> None:
        est = QueueEstimate(
            position_in_queue=1,
            estimated_start=datetime(2026, 1, 1, tzinfo=UTC),
            estimated_complete=datetime(2026, 1, 1, tzinfo=UTC),
            next_window_start=None,
            queue_summary="test",
        )
        assert est.position_in_queue == 1
        assert est.queue_summary == "test"
