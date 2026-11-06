"""Every periodic task, and the ARQ cron jobs that run them.

The one list is :data:`TASKS`. :func:`cron_jobs` turns the enabled ones into
ARQ ``CronJob`` objects for ``worker.WorkerSettings.cron_jobs``; a task
switched off in the settings is not scheduled at all.

Each cron job:

* has a FIXED ARQ job id, ``periodic:<name>``: while one run of a task is
  queued or running, ARQ refuses to enqueue another. Missed ticks therefore
  coalesce into one run — the worker runs one job at a time
  (``worker_max_jobs = 1``), so a long processing job delays a periodic task
  but never piles its ticks up — and two workers never queue the same task
  twice. The advisory lock (``runner``) covers what ARQ cannot see, such as a
  manual run from ``scripts/billing_admin.py``.
* is tried once (``max_tries=1``): a failure waits for the next tick. The
  runner catches every error anyway, so ARQ sees a failed job only on a crash.
* keeps no result, and is cut off after :data:`CRON_TIMEOUT_S`.
* creates no ``jobs`` row — a periodic task is not a Job (the seam and queue
  locks read ``functions``, not ``cron_jobs``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from arq.cron import CronJob, cron

from course_supporter.billing.periodic import BILLING_TASKS
from course_supporter.periodic.runner import PeriodicTask, run_task
from course_supporter.periodic.schedule import parse_cron

if TYPE_CHECKING:
    from course_supporter.config import Settings

TASKS: tuple[PeriodicTask, ...] = (*BILLING_TASKS,)

CRON_TIMEOUT_S = 1800
"""A periodic run longer than this is cancelled (and logged as such)."""


def task_by_name(name: str) -> PeriodicTask:
    """The registered task called ``name``.

    Raises:
        KeyError: no such task.
    """
    for task in TASKS:
        if task.name == name:
            return task
    raise KeyError(name)


def job_name(task: PeriodicTask) -> str:
    """The ARQ job name and id of the task's cron job."""
    return f"periodic:{task.name}"


def _coroutine(task: PeriodicTask) -> Any:
    async def run(ctx: dict[str, Any]) -> None:
        await run_task(task, ctx["session_factory"])

    run.__name__ = run.__qualname__ = f"periodic_{task.name}"
    return run


def cron_job(task: PeriodicTask, settings: Settings) -> CronJob:
    """The ARQ cron job of one task, on its schedule from the settings."""
    schedule = parse_cron(task.cron(settings))
    return cron(
        _coroutine(task),
        name=job_name(task),
        job_id=job_name(task),
        minute=set(schedule.minute) if schedule.minute is not None else None,
        hour=set(schedule.hour) if schedule.hour is not None else None,
        day=set(schedule.day) if schedule.day is not None else None,
        month=set(schedule.month) if schedule.month is not None else None,
        weekday=set(schedule.weekday) if schedule.weekday is not None else None,
        max_tries=1,
        keep_result=0,
        timeout=CRON_TIMEOUT_S,
    )


def cron_jobs(settings: Settings) -> list[CronJob]:
    """The cron jobs of every enabled task (module docstring)."""
    return [cron_job(task, settings) for task in TASKS if task.enabled(settings)]
