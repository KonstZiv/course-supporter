"""Job-domain primitives (vision §3 KD13).

Re-exports the canonical :class:`JobType` enum, the
:func:`validate_job_type` helper, and the cooperative-cancel
primitives :class:`JobCancellationChecker` /
:class:`JobCancelledError` so callers can write::

    from course_supporter.jobs import (
        JobCancellationChecker,
        JobCancelledError,
        JobType,
        validate_job_type,
    )
"""

from course_supporter.jobs.cancellation import (
    JobCancellationChecker,
    JobCancelledError,
)
from course_supporter.jobs.job_type import JobType, validate_job_type

__all__ = [
    "JobCancellationChecker",
    "JobCancelledError",
    "JobType",
    "validate_job_type",
]
