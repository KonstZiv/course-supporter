"""The scheduler of periodic tasks (BIL-B part 2) without infrastructure.

Locks: the schedule's reading of cron fields, the registration of the cron jobs
in ``WorkerSettings`` (the list of tasks and their schedules), the switches,
the one log line of a run, and that a failing task never escapes its runner.
The tasks' work on a live database is
``tests/integration/test_billing_periodic_db.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from course_supporter.config import Settings
from course_supporter.periodic import registry
from course_supporter.periodic.registry import CRON_TIMEOUT_S, cron_jobs, job_name
from course_supporter.periodic.runner import (
    PeriodicTask,
    TaskBusyError,
    TaskContext,
    TaskResult,
    lock_key,
    run_task,
)
from course_supporter.periodic.schedule import CronSchedule, parse_cron
from course_supporter.worker import HomeworkWorkerSettings, WorkerSettings

ALL = None


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _task(body: Any, name: str = "probe") -> PeriodicTask:
    return PeriodicTask(
        name=name,
        body=body,
        enabled=lambda s: True,
        cron=lambda s: "0 0 1 1 *",
        summary="a probe",
    )


# ── Schedule ───────────────────────────────────────────────────────────


class TestParseCron:
    def test_every_hour_at_a_minute(self) -> None:
        assert parse_cron("5 * * * *") == CronSchedule(
            minute=frozenset({5}), hour=ALL, day=ALL, month=ALL, weekday=ALL
        )

    def test_a_step(self) -> None:
        assert parse_cron("*/15 * * * *").minute == {0, 15, 30, 45}

    def test_a_step_from_a_start_and_in_a_range(self) -> None:
        assert parse_cron("5/20 * * * *").minute == {5, 25, 45}
        assert parse_cron("10-30/10 * * * *").minute == {10, 20, 30}

    def test_lists_and_ranges(self) -> None:
        assert parse_cron("0 1,3-5 * * *").hour == {1, 3, 4, 5}

    def test_cron_weekdays_become_arqs(self) -> None:
        """cron: Sunday 0 or 7, Monday 1; ARQ (Python): Monday 0, Sunday 6."""
        assert parse_cron("0 3 * * 0").weekday == {6}
        assert parse_cron("0 3 * * 7").weekday == {6}
        assert parse_cron("0 3 * * 1-5").weekday == {0, 1, 2, 3, 4}

    @pytest.mark.parametrize(
        "text",
        [
            "* * * *",  # four fields
            "60 * * * *",  # minute out of range
            "0 24 * * *",  # hour out of range
            "0 0 0 * *",  # day 0
            "*/0 * * * *",  # step 0
            "30-10 * * * *",  # backwards
            "x * * * *",
            "0 3 1 * 1",  # both the day and the weekday
        ],
    )
    def test_refused(self, text: str) -> None:
        with pytest.raises(ValueError):
            parse_cron(text)


# ── Settings ───────────────────────────────────────────────────────────


class TestSettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "BILLING_STALE_RESERVE_HOURS",
            "BILLING_RETENTION_YEARS",
            "BILLING_PURGE_BATCH_SIZE",
        ):
            monkeypatch.delenv(name, raising=False)
        s = _settings()
        assert s.billing_stale_reserve_hours == 24
        assert s.billing_retention_years == 5
        assert s.billing_purge_batch_size == 50

    def test_a_bad_schedule_stops_the_boot(self) -> None:
        with pytest.raises(ValidationError, match="periodic_billing_purge_cron"):
            _settings(periodic_billing_purge_cron="every night")

    @pytest.mark.parametrize(
        "field",
        ["billing_stale_reserve_hours", "billing_retention_years"],
    )
    def test_a_zero_threshold_is_refused(self, field: str) -> None:
        """A zero retention would destroy accounts the day they are anonymized."""
        with pytest.raises(ValidationError):
            _settings(**{field: 0})


# ── Registration in the worker (lock: the list of tasks and the schedule) ──

DEFAULT_SCHEDULE = {
    "periodic:billing_expire": ({5}, None),
    "periodic:billing_annul": ({10}, None),
    "periodic:billing_stale_reserves": ({0, 15, 30, 45}, None),
    "periodic:billing_purge": ({30}, {3}),
}


class TestRegistration:
    def test_the_default_worker_runs_the_four_billing_tasks(self) -> None:
        jobs = {job.name: job for job in WorkerSettings.cron_jobs}
        assert {n: (j.minute, j.hour) for n, j in jobs.items()} == DEFAULT_SCHEDULE
        for name, job in jobs.items():
            assert job.job_id == name  # a fixed id: ticks coalesce, never pile up
            assert job.day is None and job.month is None and job.weekday is None
            assert job.max_tries == 1
            assert job.keep_result_s == 0
            assert job.timeout_s == CRON_TIMEOUT_S
            assert not job.run_at_startup

    def test_the_schedule_is_utc(self) -> None:
        assert WorkerSettings.timezone is UTC

    def test_the_homework_worker_runs_none(self) -> None:
        assert not getattr(HomeworkWorkerSettings, "cron_jobs", None)

    def test_cron_jobs_stay_out_of_functions(self) -> None:
        """The seam and queue locks read ``functions``; a periodic task is no Job."""
        names = {getattr(f, "__name__", "") for f in WorkerSettings.functions}
        assert not any(n.startswith("periodic") for n in names)

    def test_built_from_the_settings(self) -> None:
        jobs = cron_jobs(_settings(periodic_billing_purge_cron="15 1 * * 0"))
        purge = next(j for j in jobs if j.name == "periodic:billing_purge")
        assert (purge.minute, purge.hour, purge.weekday) == ({15}, {1}, {6})

    def test_a_task_switched_off_is_not_scheduled(self) -> None:
        jobs = cron_jobs(
            _settings(
                periodic_billing_purge_enabled=False,
                periodic_billing_stale_reserves_enabled=False,
            )
        )
        assert [j.name for j in jobs] == [
            "periodic:billing_expire",
            "periodic:billing_annul",
        ]

    def test_every_task_has_a_switch_and_a_schedule(self) -> None:
        s = _settings()
        for task in registry.TASKS:
            assert getattr(s, f"periodic_{task.name}_enabled") is task.enabled(s)
            assert getattr(s, f"periodic_{task.name}_cron") == task.cron(s)
            assert registry.task_by_name(task.name) is task
            assert job_name(task) == f"periodic:{task.name}"

    def test_names_and_locks_are_unique(self) -> None:
        names = [t.name for t in registry.TASKS]
        assert len(set(names)) == len(names)
        assert len({lock_key(n) for n in names}) == len(names)

    def test_the_lock_key_is_stable(self) -> None:
        """Two processes must agree on the lock: no per-process hash salt."""
        assert lock_key("billing_purge") == lock_key("billing_purge")
        assert -(2**63) <= lock_key("billing_purge") < 2**63


# ── One run: one log line; a failure never escapes ─────────────────────


class TestRunTask:
    async def test_a_run_logs_one_line_with_its_counts(self) -> None:
        async def body(ctx: TaskContext) -> TaskResult:
            return TaskResult(3, {"units": 120})

        with capture_logs() as logs:
            run = await run_task(_task(body), MagicMock(), settings=_settings())
        assert (run.processed, run.error, run.skipped) == (3, None, None)
        lines = [e for e in logs if e["event"] == "periodic_task_run"]
        assert len(lines) == 1
        line = lines[0]
        assert line["log_level"] == "info"
        assert line["task"] == "probe"
        assert line["processed"] == 3
        assert line["units"] == 120
        assert line["error"] is None
        assert isinstance(line["duration_ms"], int)

    async def test_a_failure_is_logged_and_does_not_escape(self) -> None:
        async def body(ctx: TaskContext) -> TaskResult:
            raise RuntimeError("database gone")

        with capture_logs() as logs:
            run = await run_task(_task(body), MagicMock(), settings=_settings())
        assert run.error == "RuntimeError: database gone"
        assert run.processed == 0
        lines = [e for e in logs if e["event"] == "periodic_task_run"]
        assert len(lines) == 1
        assert lines[0]["log_level"] == "error"
        assert lines[0]["error"] == "RuntimeError: database gone"

    async def test_a_busy_lock_is_a_skip_not_an_error(self) -> None:
        async def body(ctx: TaskContext) -> TaskResult:
            raise TaskBusyError(ctx.task_name)

        with capture_logs() as logs:
            run = await run_task(_task(body), MagicMock(), settings=_settings())
        assert (run.skipped, run.error) == ("busy", None)
        assert [e["log_level"] for e in logs if e["event"] == "periodic_task_run"] == [
            "info"
        ]

    async def test_a_cancellation_is_logged_and_passed_on(self) -> None:
        """ARQ's timeout cancels the job: the run says so, the job still ends."""

        async def body(ctx: TaskContext) -> TaskResult:
            raise asyncio.CancelledError

        with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
            await run_task(_task(body), MagicMock(), settings=_settings())
        assert [e["error"] for e in logs if e["event"] == "periodic_task_run"] == [
            "cancelled"
        ]

    async def test_the_body_gets_the_dry_run_and_the_settings(self) -> None:
        seen: list[TaskContext] = []

        async def body(ctx: TaskContext) -> TaskResult:
            seen.append(ctx)
            return TaskResult(0)

        s = _settings(billing_retention_years=7)
        await run_task(_task(body), MagicMock(), settings=s, dry_run=True)
        assert seen[0].dry_run is True
        assert seen[0].settings.billing_retention_years == 7
        assert seen[0].now.tzinfo is not None


class TestCronCoroutine:
    async def test_a_failing_task_does_not_fail_its_job_or_the_next(self) -> None:
        """Each cron job runs through the runner: one task's error stays in its
        log line, and the next task runs as usual."""
        ran: list[str] = []

        async def failing(ctx: TaskContext) -> TaskResult:
            ran.append("failing")
            raise RuntimeError("boom")

        async def fine(ctx: TaskContext) -> TaskResult:
            ran.append("fine")
            return TaskResult(1)

        s = _settings()
        jobs = [
            registry.cron_job(_task(failing, "failing"), s),
            registry.cron_job(_task(fine, "fine"), s),
        ]
        ctx = {"session_factory": MagicMock()}
        with capture_logs() as logs:
            for job in jobs:
                await job.coroutine(ctx)  # neither raises
        assert ran == ["failing", "fine"]
        levels = {e["task"]: e["log_level"] for e in logs if "task" in e}
        assert levels == {"failing": "error", "fine": "info"}
