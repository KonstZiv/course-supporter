"""Canonical Job type enum + transitional application-level validation.

Vision §3 KD13 fixes the KD13 universe of ``Job.job_type`` values to
four; KD18 (P2) adds a fifth deterministic type:

* ``document_processing`` — pipeline of one ``AuthoredDocument``
  (stages: pass_1 → pass_2a → pass_2b → pass_2c).
* ``node_summary_regeneration`` — bottom-up + top-down generation
  over a ``CourseNode`` subtree.
* ``homework_processing`` — student submission review (safety →
  sanity → review → delivery).
* ``s3_cleanup`` — async hard-delete of S3 files after soft-delete.
* ``base_normalize`` — deterministic project-base archive
  normalization (KD18 P2; zero LLM, no ESC).

The DB-level CHECK constraint that would enforce this is
**deferred to Phase 2.x** (see task 0.3 pre-flight): current
call-sites in ``enqueue.py`` emit legacy strings (``ingest``,
etc.) outside the KD13 enum, and a strict CHECK would break
dev/test/CI immediately.

This module ships the **transitional** layer: a clean StrEnum that
matches KD13 exactly + an opt-in validator that lets callers signal
KD13-awareness via the enum type while accepting legacy strings
with a one-shot ``DeprecationWarning``. Phase 2.x will rewrite the
call-sites onto :class:`JobType` and drop the ``str`` arm of the
union (and at that point the DB CHECK becomes safe to add).
"""

from __future__ import annotations

import warnings
from enum import StrEnum


class JobType(StrEnum):
    """Canonical Job types per vision §3 KD13.

    Use this enum from new call-sites. The string values are stable
    and equal to the enum members (``StrEnum``), so JSONB / DB
    storage is unaffected by the type system change.
    """

    DOCUMENT_PROCESSING = "document_processing"
    NODE_SUMMARY_REGENERATION = "node_summary_regeneration"
    HOMEWORK_PROCESSING = "homework_processing"
    S3_CLEANUP = "s3_cleanup"
    # KD18 P2: deterministic base-archive normalization (zero LLM, no ESC).
    # A String(50) job_type value — no migration (the DB CHECK is still
    # deferred, see module docstring). The one Job type added by KD18.
    BASE_NORMALIZE = "base_normalize"


_CANONICAL_VALUES: frozenset[str] = frozenset(jt.value for jt in JobType)
_LEGACY_JOB_TYPES_WARNED: set[str] = set()


def validate_job_type(value: JobType | str) -> str:
    """Normalize a ``job_type`` argument to its underlying string.

    The accepted shapes:

    * :class:`JobType` enum member — silent accept (KD13-canonical).
    * ``str`` equal to one of :class:`JobType`'s values — silent
      accept (caller used the canonical name as a string; equivalent
      to passing the enum).
    * Any other ``str`` — accepted with a single
      :class:`DeprecationWarning` per distinct value per process.
      This covers legacy ``Job.job_type`` strings (``ingest``,
      ``generate_structure``, ``methodist_bottomup``, etc.) until
      Phase 2.x rewrites them.

    The warning is intentionally one-shot per value (module-level
    ``set`` dedup) to keep test runs and worker logs free of noise
    while still surfacing each distinct legacy string once. Python's
    own ``warnings`` filter would dedup per call-site, but the same
    legacy string is emitted from many call-sites — value-level
    dedup matches the actual cleanup target (one Phase 2.x rewrite
    per distinct legacy value).

    Returns the underlying ``str`` for storage. Never raises — strict
    rejection is deferred to the Phase 2.x DB CHECK constraint.
    """
    if isinstance(value, JobType):
        return value.value
    if value in _CANONICAL_VALUES:
        return value
    if value not in _LEGACY_JOB_TYPES_WARNED:
        _LEGACY_JOB_TYPES_WARNED.add(value)
        warnings.warn(
            f"Legacy Job.job_type {value!r} accepted; rewrite to "
            f"course_supporter.jobs.JobType in Phase 2.x. Canonical "
            f"values: {sorted(_CANONICAL_VALUES)}.",
            DeprecationWarning,
            stacklevel=3,
        )
    return value
