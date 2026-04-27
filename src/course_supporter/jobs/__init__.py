"""Job-domain primitives (vision §3 KD13).

Re-exports the canonical :class:`JobType` enum and the
:func:`validate_job_type` helper so callers can write::

    from course_supporter.jobs import JobType, validate_job_type
"""

from course_supporter.jobs.job_type import JobType, validate_job_type

__all__ = ["JobType", "validate_job_type"]
