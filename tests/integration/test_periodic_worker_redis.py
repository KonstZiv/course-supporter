"""The periodic tasks in a real ARQ worker, on the tests' own Redis database.

A failing task fails neither its job nor the worker, and the next task runs;
while a run of a task waits in the queue, another tick of it is not queued
(the fixed job id), so missed ticks never pile up behind a long job.

Run with ``--run-redis``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from arq.connections import ArqRedis
from arq.worker import Worker

from course_supporter.billing.periodic import BILLING_EXPIRE
from course_supporter.config import get_settings
from course_supporter.periodic import registry
from course_supporter.periodic.runner import PeriodicTask, TaskContext, TaskResult
from tests._helpers.redis_isolation import redis_test_settings

pytestmark = pytest.mark.requires_redis


def _task(name: str, body: Any) -> PeriodicTask:
    return PeriodicTask(
        name=name,
        body=body,
        enabled=lambda s: True,
        cron=lambda s: "0 0 1 1 *",  # never due during the test
        summary=name,
    )


async def test_a_failing_task_stops_neither_the_worker_nor_the_next(
    arq_redis: ArqRedis,
) -> None:
    ran: list[str] = []

    async def failing(ctx: TaskContext) -> TaskResult:
        ran.append("failing")
        raise RuntimeError("boom")

    async def fine(ctx: TaskContext) -> TaskResult:
        ran.append("fine")
        return TaskResult(1)

    settings = get_settings()
    jobs = [
        registry.cron_job(_task("failing", failing), settings),
        registry.cron_job(_task("fine", fine), settings),
    ]
    for job in jobs:
        assert await arq_redis.enqueue_job(job.name, _job_id=job.job_id)
    worker = Worker(
        functions=[],
        cron_jobs=jobs,
        redis_settings=redis_test_settings(),
        ctx={"session_factory": MagicMock()},
        burst=True,
        handle_signals=False,
        poll_delay=0.05,
        max_jobs=1,
    )
    try:
        await worker.async_run()
    finally:
        await worker.close()

    assert sorted(ran) == ["failing", "fine"]
    assert (worker.jobs_complete, worker.jobs_failed) == (2, 0)


async def test_a_tick_is_not_queued_while_a_run_waits(arq_redis: ArqRedis) -> None:
    job = registry.cron_job(BILLING_EXPIRE, get_settings())

    first = await arq_redis.enqueue_job(job.name, _job_id=job.job_id)
    second = await arq_redis.enqueue_job(job.name, _job_id=job.job_id)

    assert first is not None
    assert second is None
