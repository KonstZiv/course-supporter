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


# ── L1b: typed job subject ──────────────────────────────────────────────
# The subject_type token per job_type — the ONE source for both the enqueue
# write side (``enqueue.py`` / ``s3_cleanup_orchestration.py``) and the
# ``ck_jobs_subject_type_legal`` DB CHECK whitelist. The migration freezes an
# equivalent SQL literal and a test-lock asserts the two agree (contract R3/R4;
# derive-or-verify, the №18 lesson). A ``None`` value means the type has no
# natural single subject — ``s3_cleanup`` operates on a raw file-key list
# (R2), so its ``subject_type``/``subject_id`` stay NULL and the idempotency
# index (``uq_jobs_subject_in_flight``) does not apply to it.
JOB_SUBJECT_TYPE: dict[JobType, str | None] = {
    JobType.DOCUMENT_PROCESSING: "authored_document",
    JobType.HOMEWORK_PROCESSING: "homework_submission",
    JobType.NODE_SUMMARY_REGENERATION: "course_node",
    JobType.BASE_NORMALIZE: "project_base",
    JobType.S3_CLEANUP: None,
}

# Totality guard (mirror of ``_STATUS_CATEGORY`` in ``job_repository``): a new
# JobType member added without an explicit subject_type — or an explicit
# ``None`` decision — fails loudly at import, not silently at the enqueue site.
# This is the L1b analogue of the L1a totality guard that a new terminal status
# cannot be forgotten the way ``running`` was.
_subjectless = set(JobType) - set(JOB_SUBJECT_TYPE)
if _subjectless:  # pragma: no cover — guarded by test_l1b_invariants
    msg = (
        f"JobType members without a JOB_SUBJECT_TYPE entry: "
        f"{sorted(jt.value for jt in _subjectless)}. Add each to "
        f"JOB_SUBJECT_TYPE (a subject_type token, or an explicit None when the "
        f"type has no natural single subject)."
    )
    raise RuntimeError(msg)

# The legal ``(job_type, subject_type)`` pairs, excluding the NULL branch —
# the source of the ``ck_jobs_subject_type_legal`` whitelist and its test-lock.
JOB_SUBJECT_TYPE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    (jt.value, st) for jt, st in JOB_SUBJECT_TYPE.items() if st is not None
)


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
