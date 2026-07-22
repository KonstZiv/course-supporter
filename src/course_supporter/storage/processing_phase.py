"""Derive the author-facing processing phase of an authored document.

L3 (root cause #2): the ``AuthoredDocument.state`` property collapses a
document with a job *standing in the queue* and one whose *worker has taken
it* into a single ``pending`` value. This module derives the finer external
``processing_phase`` — a NEW sibling of ``state`` (Рат.3), never a mutation of
it — that splits the in-flight case into ``queued`` vs ``processing``. №21 adds
one more non-terminal phase, ``awaiting_author`` (see :func:`is_awaiting_author`),
for a prep proposal the author has not yet confirmed.

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

from collections.abc import Mapping
from typing import Any, Literal

ProcessingPhase = Literal["queued", "processing", "awaiting_author", "ready", "error"]


def derive_processing_phase(
    error_message: str | None,
    pending_job_status: str | None,
    awaiting_author: bool = False,
) -> ProcessingPhase:
    """Map (error, in-flight Job status, awaiting-author signal) → phase.

    Deterministic precedence (Рат.3; №21 inserts ``awaiting_author`` between the
    in-flight split and ``ready``):

    * a non-empty ``error_message`` → ``error`` (mirrors ``state`` verbatim);
    * otherwise an in-flight ``pending_job`` splits by ``Job.status``:
      ``queued`` → ``queued`` (worker has not taken it),
      ``active`` → ``processing`` (worker took it);
    * otherwise ``awaiting_author`` (№21) → ``awaiting_author``: prep produced a
      file-role proposal the author has not yet covered (see
      :func:`is_awaiting_author`). Deliberately ABOVE ``ready`` — a retry of a
      material that still carries a live old summary must show the confirmation
      screen, not a stale ``ready``;
    * otherwise → ``ready``. This covers ``None`` (no in-flight job) AND any
      at-rest terminal (``complete`` / ``failed`` / ``cancelled`` /
      ``obsolete``): a stale terminal ``job_id`` on a live document is an
      anomaly and is treated as "no job" — never a lying in-flight phase.

    The in-flight phases and ``awaiting_author`` are the non-terminal values; the
    terminal values (``ready`` / ``error``) mirror ``state`` verbatim. The tokens
    are internal API values — human phrasing is a separate FE layer (contract §3
    «Мова назовні»).

    Args:
        error_message: ``AuthoredDocument.error_message`` (``None`` if clean).
        pending_job_status: ``pending_job.status`` of the in-flight ingestion
            job, or ``None`` when the document carries no job.
        awaiting_author: the №21 file-role signal — ``True`` when a proposal is
            present and not yet covered by the author's decision. Outranked by an
            in-flight job (a running prep/processing shows queued/processing).

    Returns:
        One of ``queued`` | ``processing`` | ``awaiting_author`` | ``ready`` |
        ``error`` — never empty (Рат.3).

    Examples:
        >>> derive_processing_phase(None, "queued")
        'queued'
        >>> derive_processing_phase(None, "active")
        'processing'
        >>> derive_processing_phase("boom", "active")
        'error'
        >>> derive_processing_phase(None, None)
        'ready'
        >>> derive_processing_phase(None, None, awaiting_author=True)
        'awaiting_author'
        >>> derive_processing_phase(None, "queued", awaiting_author=True)
        'queued'
    """
    if error_message:
        return "error"
    if pending_job_status == "queued":
        return "queued"
    if pending_job_status == "active":
        return "processing"
    if awaiting_author:
        return "awaiting_author"
    return "ready"


def is_awaiting_author(file_roles: Mapping[str, Any] | None) -> bool:
    """Whether a document waits on the author's file-role decision (№21).

    ``True`` when prep produced a ``proposal`` the author has NOT yet covered: a
    proposal exists AND either there is no ``decision``, or the decision's
    ``tree_digest`` no longer matches the proposal's (the file tree changed under
    a saved decision). Inverse of the chain condition in
    ``_prepare_on_terminal``: when the decision covers the proposal, prep chains
    straight to processing and there is nothing to wait on.

    The "no in-flight job" half of the awaiting condition is NOT here — it is the
    :func:`derive_processing_phase` precedence's job (an in-flight ``queued`` /
    ``processing`` outranks ``awaiting_author``). This signal is purely "a
    proposal is present and not covered by a matching decision".
    """
    if not file_roles:
        return False
    proposal = file_roles.get("proposal")
    if not proposal:
        return False
    decision = file_roles.get("decision")
    if not decision:
        return True
    return bool(decision.get("tree_digest") != proposal.get("tree_digest"))
