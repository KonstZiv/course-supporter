"""Periodic tasks: the system's one scheduler (ARQ ``cron_jobs``).

Purpose:
    Work that runs on the clock rather than on a request — today the four
    billing tasks of BIL-B part 2; any direction adds its own the same way.

Where it runs:
    In ONE worker, ``worker.WorkerSettings`` (the default queue, the
    ``worker`` service of ``docker-compose.prod.yaml``). Not in
    ``HomeworkWorkerSettings``: that queue serves students' submissions and
    nothing else.

Modules:
    :mod:`.schedule` — five cron fields (UTC) read into ARQ's terms.
    :mod:`.runner` — one run: the advisory lock, the transaction, the one
    structured log line; an error never escapes it.
    :mod:`.registry` — the list of tasks and the cron jobs built from it.

Adding a task:
    a body ``async (TaskContext) -> TaskResult`` that opens its transactions
    with ``context.transaction()``; two settings, ``periodic_<name>_enabled``
    and ``periodic_<name>_cron``; a :class:`.runner.PeriodicTask` in
    :data:`.registry.TASKS`.
"""
