"""Completeness lock: every Job-bearing ARQ task goes through the L2 seam.

job-lifecycle-contract §L2 (Рат.4) + impl-rules#13 (full list, not a subset). The
registry is the UNION of both worker classes — ``WorkerSettings.functions`` and
``HomeworkWorkerSettings.functions`` (homework runs on its own queue). Every
registered function whose enqueue creates a ``Job`` row MUST be wrapped by
``through_seam`` (carries ``__seam_wrapped__``); the sole fire-and-forget 4th form
(``arq_send_email`` — no Job row) is excluded EXPLICITLY, not by a handwritten
allowlist of the wrapped ones.
"""

from __future__ import annotations

from course_supporter.jobs.job_type import JobType
from course_supporter.worker import (
    HomeworkWorkerSettings,
    WorkerSettings,
    arq_send_email,
)

# The fire-and-forget 4th form: registered on the worker but creates NO Job row
# (``enqueue_email`` issues a bare ``arq.enqueue_job``), so the seam does not
# apply. Named explicitly so a NEW Job-bearing task cannot hide behind a silent
# subset — it is Job-bearing unless it is on this list.
_FIRE_AND_FORGET = frozenset({arq_send_email})

_REGISTERED = frozenset(WorkerSettings.functions) | frozenset(
    HomeworkWorkerSettings.functions
)


def _is_seam_wrapped(fn: object) -> bool:
    return getattr(fn, "__seam_wrapped__", False) is True


def test_fire_and_forget_set_is_registered_and_unwrapped() -> None:
    """The exclusion set is real (registered) and genuinely not seam-wrapped —
    so it cannot rot into hiding a Job-bearing task."""
    assert _FIRE_AND_FORGET <= _REGISTERED
    for fn in _FIRE_AND_FORGET:
        assert not _is_seam_wrapped(fn)


def test_every_job_bearing_registered_task_is_seam_wrapped() -> None:
    """Every registered task except the fire-and-forget form goes through the
    seam (impl-rules#13: the FULL union, not a subset of one settings class)."""
    job_bearing = _REGISTERED - _FIRE_AND_FORGET
    unwrapped = sorted(
        getattr(fn, "__name__", repr(fn))
        for fn in job_bearing
        if not _is_seam_wrapped(fn)
    )
    assert not unwrapped, f"Job-bearing tasks not routed through the seam: {unwrapped}"


def test_job_bearing_count_matches_job_type_universe() -> None:
    """Ties completeness to the single source: there is exactly one Job-bearing
    registered task per ``JobType`` member (every JobType has a JOB_SUBJECT_TYPE
    entry). A new JobType + task that forgets registration/wrapping — or a stray
    registered task with no JobType — breaks this count."""
    job_bearing = _REGISTERED - _FIRE_AND_FORGET
    assert len(job_bearing) == len(JobType)
