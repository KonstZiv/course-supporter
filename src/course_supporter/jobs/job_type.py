"""Canonical Job type enum + application-level validation.

Vision §3 KD13 fixes the universe of ``Job.job_type`` values to five:

* ``document_processing`` — pipeline of one ``AuthoredDocument``
  (stages: pass_1 → pass_2a → pass_2b → pass_2c).
* ``node_summary_regeneration`` — bottom-up + top-down generation
  over a ``CourseNode`` subtree.
* ``homework_processing`` — student submission review (safety →
  sanity → review → delivery).
* ``s3_cleanup`` — async hard-delete of S3 files after soft-delete.
* ``base_normalize`` — deterministic project-base archive
  normalization (KD18 P2; zero LLM, no ESC).

The DB-level CHECK constraint ``ck_jobs_job_type`` (L1a migration)
enforces exactly this set at the storage layer. :func:`validate_job_type`
is the application-level mirror: it gives a clean ``ValueError`` at the
call site before the INSERT rather than an opaque ``IntegrityError`` from
the CHECK. The transitional ``DeprecationWarning`` arm that accepted
legacy strings (``ingest`` / ``homework``) is gone — the L1a data
migration rewrote those values and all call-sites now emit the enum.
"""

from __future__ import annotations

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


def validate_job_type(value: JobType | str) -> str:
    """Normalize a ``job_type`` argument to its canonical string.

    Accepts a :class:`JobType` enum member or a ``str`` equal to one
    of its canonical values; returns the underlying string for storage.

    Raises:
        ValueError: If ``value`` is a string outside the canonical set.
            The DB CHECK ``ck_jobs_job_type`` enforces the same set; this
            gives a clean write-site error instead of an ``IntegrityError``
            surfacing from the flush.
    """
    if isinstance(value, JobType):
        return value.value
    if value in _CANONICAL_VALUES:
        return value
    msg = f"Unknown job_type {value!r}; canonical values: {sorted(_CANONICAL_VALUES)}."
    raise ValueError(msg)
