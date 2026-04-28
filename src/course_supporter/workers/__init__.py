"""ARQ worker task functions.

Modules in this package define ``async def arq_*`` /
``*_task`` functions that ARQ invokes from queue messages. Each
function is registered in ``course_supporter.worker.WorkerSettings``
(or ``HomeworkWorkerSettings``) ``functions`` list.

Per project convention, ARQ tasks live here for vision-aligned
concerns (KD13 cancel/cleanup) — but pre-Phase-2.x pipeline
workers still live in ``course_supporter.api.tasks`` for
historical reasons. The Phase 2.x rewrite collapses them into
this package.
"""
