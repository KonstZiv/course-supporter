"""Tests for WorkWindow service."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from course_supporter.worker_window import WorkWindow

KYIV = ZoneInfo("Europe/Kyiv")


def _window(
    start: time = time(2, 0),
    end: time = time(6, 30),
    tz: str = "UTC",
    enabled: bool = True,
) -> WorkWindow:
    return WorkWindow.from_settings(start=start, end=end, tz_name=tz, enabled=enabled)


def _with_now(window: WorkWindow, now: datetime) -> WorkWindow:
    """Return a copy of window whose now() returns a fixed datetime."""

    class Fixed(WorkWindow):
        def now(self) -> datetime:
            return now

    return Fixed(
        start=window.start,
        end=window.end,
        tz=window.tz,
        enabled=window.enabled,
    )


# ── Disabled mode (24/7) ──


class TestDisabledWindow:
    def test_always_active(self) -> None:
        w = _window(enabled=False)
        now = datetime(2026, 2, 19, 12, 0, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is True

    def test_always_active_at_midnight(self) -> None:
        w = _window(enabled=False)
        now = datetime(2026, 2, 19, 0, 0, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is True

    def test_next_start_returns_now(self) -> None:
        w = _window(enabled=False)
        now = datetime(2026, 2, 19, 15, 0, tzinfo=UTC)
        fixed = _with_now(w, now)
        assert fixed.next_start() == now

    def test_remaining_24h(self) -> None:
        w = _window(enabled=False)
        now = datetime(2026, 2, 19, 15, 0, tzinfo=UTC)
        assert _with_now(w, now).remaining_today() == timedelta(hours=24)


# ── Regular window (02:00 → 06:30) ──


class TestRegularWindow:
    def test_active_inside(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is True

    def test_active_at_start_boundary(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 2, 0, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is True

    def test_inactive_at_end_boundary(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 6, 30, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is False

    def test_inactive_before_start(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 1, 59, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is False

    def test_inactive_after_end(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 10, 0, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is False

    def test_not_overnight(self) -> None:
        assert _window().is_overnight is False


# ── Overnight window (22:00 → 06:00) ──


class TestOvernightWindow:
    def test_is_overnight(self) -> None:
        w = _window(start=time(22, 0), end=time(6, 0))
        assert w.is_overnight is True

    def test_active_before_midnight(self) -> None:
        w = _window(start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 2, 19, 23, 0, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is True

    def test_active_after_midnight(self) -> None:
        w = _window(start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 2, 20, 3, 0, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is True

    def test_active_at_start(self) -> None:
        w = _window(start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 2, 19, 22, 0, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is True

    def test_inactive_at_end(self) -> None:
        w = _window(start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 2, 20, 6, 0, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is False

    def test_inactive_midday(self) -> None:
        w = _window(start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 2, 19, 12, 0, tzinfo=UTC)
        assert _with_now(w, now).is_active_now() is False


# ── next_start() ──


class TestNextStart:
    def test_before_start_returns_today(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 1, 0, tzinfo=UTC)
        result = _with_now(w, now).next_start()
        assert result == datetime(2026, 2, 19, 2, 0, tzinfo=UTC)

    def test_during_window_returns_tomorrow(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        result = _with_now(w, now).next_start()
        assert result == datetime(2026, 2, 20, 2, 0, tzinfo=UTC)

    def test_overnight_morning_part_returns_today_start(self) -> None:
        """At 03:00 inside overnight 22:00→06:00, next opening is today 22:00."""
        w = _window(start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 2, 19, 3, 0, tzinfo=UTC)
        result = _with_now(w, now).next_start()
        assert result == datetime(2026, 2, 19, 22, 0, tzinfo=UTC)

    def test_overnight_evening_part_returns_tomorrow(self) -> None:
        """At 23:00 inside overnight 22:00→06:00, next opening is tomorrow 22:00."""
        w = _window(start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 2, 19, 23, 0, tzinfo=UTC)
        result = _with_now(w, now).next_start()
        assert result == datetime(2026, 2, 20, 22, 0, tzinfo=UTC)

    def test_overnight_outside_returns_today_start(self) -> None:
        """At 12:00 outside overnight 22:00→06:00, next opening is today 22:00."""
        w = _window(start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 2, 19, 12, 0, tzinfo=UTC)
        result = _with_now(w, now).next_start()
        assert result == datetime(2026, 2, 19, 22, 0, tzinfo=UTC)

    def test_after_window_returns_tomorrow(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 10, 0, tzinfo=UTC)
        result = _with_now(w, now).next_start()
        assert result == datetime(2026, 2, 20, 2, 0, tzinfo=UTC)


# ── remaining_today() ──


class TestRemainingToday:
    def test_inside_window(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 4, 0, tzinfo=UTC)
        remaining = _with_now(w, now).remaining_today()
        assert remaining == timedelta(hours=2, minutes=30)

    def test_at_start(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 2, 0, tzinfo=UTC)
        remaining = _with_now(w, now).remaining_today()
        assert remaining == timedelta(hours=4, minutes=30)

    def test_outside_window_returns_zero(self) -> None:
        w = _window()
        now = datetime(2026, 2, 19, 10, 0, tzinfo=UTC)
        assert _with_now(w, now).remaining_today() == timedelta(0)

    def test_overnight_before_midnight(self) -> None:
        w = _window(start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 2, 19, 23, 0, tzinfo=UTC)
        remaining = _with_now(w, now).remaining_today()
        assert remaining == timedelta(hours=7)

    def test_overnight_after_midnight(self) -> None:
        w = _window(start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 2, 20, 3, 0, tzinfo=UTC)
        remaining = _with_now(w, now).remaining_today()
        assert remaining == timedelta(hours=3)


# ── Timezone handling ──


class TestTimezone:
    def test_kyiv_timezone(self) -> None:
        w = _window(start=time(2, 0), end=time(6, 30), tz="Europe/Kyiv")
        # 04:00 Kyiv = 02:00 UTC (in winter, UTC+2)
        now = datetime(2026, 2, 19, 4, 0, tzinfo=KYIV)
        assert _with_now(w, now).is_active_now() is True

    def test_from_settings_factory(self) -> None:
        w = WorkWindow.from_settings(
            start=time(2, 0),
            end=time(6, 30),
            tz_name="Europe/Kyiv",
            enabled=True,
        )
        assert w.tz == KYIV
        assert w.enabled is True
