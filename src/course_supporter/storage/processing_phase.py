"""Derive the author-facing processing phase of an authored document.

L3 (root cause #2): the ``AuthoredDocument.state`` property collapses a
document with a job *standing in the queue* and one whose *worker has taken
it* into a single ``pending`` value. This module derives the finer external
``processing_phase`` — a NEW sibling of ``state`` (Рат.3), never a mutation of
it — that splits the in-flight case into ``queued`` vs ``processing``.

The function is deliberately PURE: it takes the two already-loaded scalars
(``error_message`` and the in-flight ``Job.status`` reached through
``pending_job``) and returns a token. It performs no session access and
triggers no lazy load — the caller is responsible for eager-loading
``pending_job`` (Рат.6). The full status matrix is locked in
``tests/unit/test_processing_phase.py`` (every ``JOB_TRANSITIONS`` key), with a
guard that fails loudly if a seventh Job status appears without a phase
decision.
"""

from __future__ import annotations

from typing import Literal

ProcessingPhase = Literal["queued", "processing", "ready", "error"]


def derive_processing_phase(
    error_message: str | None,
    pending_job_status: str | None,
) -> ProcessingPhase:
    """Map (error, in-flight Job status) → external processing phase.

    Deterministic precedence (Рат.3):

    * a non-empty ``error_message`` → ``error`` (mirrors ``state`` verbatim);
    * otherwise an in-flight ``pending_job`` splits by ``Job.status``:
      ``queued`` → ``queued`` (worker has not taken it),
      ``active`` → ``processing`` (worker took it);
    * otherwise → ``ready``. This covers ``None`` (no in-flight job) AND any
      at-rest terminal (``complete`` / ``failed`` / ``cancelled`` /
      ``obsolete``): a stale terminal ``job_id`` on a live document is an
      anomaly and is treated as "no job" — never a lying in-flight phase.

    The two in-flight phases are the ONLY non-terminal values; the terminal
    values (``ready`` / ``error``) mirror ``state`` verbatim, so the two fields
    cannot structurally contradict. The tokens are internal API values — human
    phrasing is a separate FE layer (contract §3 «Мова назовні»).

    Args:
        error_message: ``AuthoredDocument.error_message`` (``None`` if clean).
        pending_job_status: ``pending_job.status`` of the in-flight ingestion
            job, or ``None`` when the document carries no job.

    Returns:
        One of ``queued`` | ``processing`` | ``ready`` | ``error`` — never
        empty (Рат.3).

    Examples:
        >>> derive_processing_phase(None, "queued")
        'queued'
        >>> derive_processing_phase(None, "active")
        'processing'
        >>> derive_processing_phase("boom", "active")
        'error'
        >>> derive_processing_phase(None, None)
        'ready'
        >>> derive_processing_phase(None, "complete")
        'ready'
    """
    if error_message:
        return "error"
    if pending_job_status == "queued":
        return "queued"
    if pending_job_status == "active":
        return "processing"
    return "ready"
