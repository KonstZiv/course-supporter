"""Job-domain primitives (vision §3 KD13).

Re-exports the canonical :class:`JobType` enum, the
:func:`validate_job_type` helper, and the cascade-bound
:class:`JobCancellationService` (concrete ``OnCancelJobs`` implementation)
so callers can write::

    from course_supporter.jobs import (
        JobCancellationService,
        JobType,
        validate_job_type,
    )
"""

from course_supporter.jobs.cancellation_service import JobCancellationService
from course_supporter.jobs.job_type import (
    JOB_SUBJECT_TYPE,
    JOB_SUBJECT_TYPE_PAIRS,
    JobType,
    validate_job_type,
)

__all__ = [
    "JOB_SUBJECT_TYPE",
    "JOB_SUBJECT_TYPE_PAIRS",
    "JobCancellationService",
    "JobType",
    "validate_job_type",
]
