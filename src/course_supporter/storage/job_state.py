"""Derive the author-facing *work state* of a Job row.

The author work-list (step A of the "honest visibility" arc) shows one row per
Job. That row speaks its OWN state axis — distinct from ``processing_phase``,
which is the canonical axis of a *material* (``AuthoredDocument``). Decision P8
(ARC §1, clarified 2026-08-05): a Job row is not a material, so it never carries
``awaiting_author`` (a material-only value); it carries a token computed purely
from ``Job.status``.

The mapping is a total function over the SIX ``Job.status`` values
(``JOB_TRANSITIONS`` keys): the two in-flight statuses map to tokens that match
the material phase words verbatim (``queued`` / ``processing``), and the four
terminal statuses map to outcome words (``ready`` / ``error`` / ``cancelled`` /
``obsolete``). The tokens are internal API values — human phrasing is a separate
FE layer (step B; language-rules).

Completeness is guarded twice, mirroring ``_STATUS_CATEGORY``
(``job_repository.py``) and ``derive_processing_phase`` (``processing_phase.py``):
an import-time totality check fails loudly if a seventh ``Job.status`` ever
appears without a token decision, and ``test_job_state`` locks the same over
every ``JOB_TRANSITIONS`` key.
"""

from __future__ import annotations

from typing import Literal

from course_supporter.storage.job_repository import JOB_TRANSITIONS

JobState = Literal["queued", "processing", "ready", "error", "cancelled", "obsolete"]

# The ONE source mapping ``Job.status`` → work-state token (step A §3). Live
# statuses reuse the material-phase words (``queued`` / ``processing``) so the
# band and the material card read identically; terminals are outcome words.
# NOT derived from the transition graph — ``failed`` has an outgoing retry edge
# yet is a terminal outcome here (``error``), the same asymmetry ``_STATUS_
# CATEGORY`` documents.
_JOB_STATE_BY_STATUS: dict[str, JobState] = {
    "queued": "queued",
    "active": "processing",
    "complete": "ready",
    "failed": "error",
    "cancelled": "cancelled",
    "obsolete": "obsolete",
}

# Import-time totality guard (mirror of ``_STATUS_CATEGORY``): a new ``Job``
# status added to ``JOB_TRANSITIONS`` without a token here fails loudly at
# import, not silently at a serialisation site with a missing row.
_unmapped = set(JOB_TRANSITIONS) - set(_JOB_STATE_BY_STATUS)
if _unmapped:  # pragma: no cover — guarded by test_job_state
    msg = (
        f"Job statuses without a _JOB_STATE_BY_STATUS token: {sorted(_unmapped)}. "
        f"Add each to _JOB_STATE_BY_STATUS (a JobState token)."
    )
    raise RuntimeError(msg)


def derive_job_state(status: str) -> JobState:
    """Map a ``Job.status`` to its author-facing work-state token.

    Total over the ``JOB_TRANSITIONS`` domain (the DB ``ck_jobs_status`` set).
    A status outside that set raises ``ValueError`` — a fail-loud mirror of
    ``validate_job_type`` rather than a silent default, since the DB CHECK makes
    it unreachable for a persisted row.

    Examples:
        >>> derive_job_state("queued")
        'queued'
        >>> derive_job_state("active")
        'processing'
        >>> derive_job_state("complete")
        'ready'
        >>> derive_job_state("failed")
        'error'
        >>> derive_job_state("cancelled")
        'cancelled'
        >>> derive_job_state("obsolete")
        'obsolete'
    """
    try:
        return _JOB_STATE_BY_STATUS[status]
    except KeyError:
        msg = (
            f"Unknown Job.status {status!r}; canonical values: "
            f"{sorted(_JOB_STATE_BY_STATUS)}."
        )
        raise ValueError(msg) from None
