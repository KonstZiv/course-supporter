"""Base-archive normalization ARQ worker (KD18 P2).

Deterministic, zero LLM, no ``ExternalServiceCall``. Consumes the P1
normalizer library and drives the ``project_bases`` row through
``pending → ready | failed(reason)``.

Payload (from :func:`course_supporter.enqueue.enqueue_base_normalize`):
``(job_id, project_base_id)``. The ``archive_key`` is re-read from the
``ProjectBase`` row — the payload carries only ids. On success the canonical
snapshot lands in S3 and the row flips to ``ready`` with the aggregate
``snapshot_hash`` (the P3 echo-match key) + the algorithmic manifest.

Job lifecycle is owned by the L2 execution seam (:func:`through_seam`): the seam
writes ``active`` on entry, ``complete`` on the returned dict, ``failed`` on a
raised exception, and ``obsolete`` when the ProjectBase subject is dead (skipping
this body entirely). This body writes only the *domain* ``project_bases`` state.

Error discipline:

* **Permanent** — a content/structural rejection (``NormalizerError`` /
  ``SecurityRejectedError``), an unresolvable archive kind, or a missing
  archive (S3 ``NoSuchKey``): flip the row to ``failed(reason)`` (domain, its
  own commit) then RAISE ``NormalizerError(reason)`` → the seam writes the Job
  ``failed`` (no ARQ retry would ever succeed).
* **Transient** — a connection-level S3 error (not ``NoSuchKey``): raise
  ``arq.Retry`` so ARQ re-runs (``max_tries``). NOTE: a bare re-raise does NOT
  retry in ARQ (only ``Retry`` / ``RetryJob`` do) and would strand the Job
  ``active`` — the previous bare re-raise here was a latent no-op-retry; the seam
  passes ``arq.Retry`` through as control flow (not a failure).

Security note: under KD18 1A the author upload path does NOT run
``run_stage1`` (its archive branch is fail-closed and would reject a real
project's non-allowlisted files). The MANDATORY structural sandbox for a base
is ``extract_archive_safely`` in classify mode, reached via ``normalize_archive``
HERE. The resource guards therefore come from the shared
``_PROJECT_NORMALIZE_LIMITS`` (normalizer package — identical for base and P3
submission normalization), NOT from a ``ContextPolicy`` (``AUTHORED_POLICY``
leaves archives disabled).
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath
from typing import Any

import structlog
from arq import Retry
from botocore.exceptions import ClientError

from course_supporter.jobs.execution_seam import through_seam
from course_supporter.normalizer import (
    _PROJECT_NORMALIZE_LIMITS,
    NormalizerError,
    manifest_to_jsonb,
    normalize_archive,
)
from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.security.stage1 import archive_kind_for_filename
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger(__name__)

# Base normalization uses the shared ``_PROJECT_NORMALIZE_LIMITS`` (normalizer
# package) — the same instance the P3 submission worker normalizes with, so base
# and submission share one accept/reject envelope + one kept_single threshold.
# Distinct from the route's raw-upload cap (100 MB compressed): these bound the
# *unpack*, not the uploaded bytes.

# S3 "object does not exist" codes — a missing archive is PERMANENT (the raw
# upload vanished), not a retry-able connection error.
_NOT_FOUND_ERROR_CODES: frozenset[str] = frozenset({"NoSuchKey", "404", "NotFound"})

# Backoff before an ARQ retry on a transient S3 connection error. Modest, so a
# flapping endpoint does not hot-loop within the bounded ``max_tries`` budget.
_TRANSIENT_RETRY_DEFER_S = 5


def _snapshot_key_for(archive_key: str) -> str:
    """Sibling snapshot key: ``.../v{n}/original.<ext>`` → ``.../v{n}/snapshot.zip``."""
    return str(PurePosixPath(archive_key).parent / "snapshot.zip")


def _failure_reason(exc: NormalizerError | SecurityRejectedError) -> str:
    """Human-readable failure reason (Decision 3).

    A ``SecurityRejectedError`` is prefixed with its ``category`` (the security
    taxonomy — e.g. ``malformed_archive`` / ``forbidden_type``); a
    ``NormalizerError`` carries its class name + message.
    """
    if isinstance(exc, SecurityRejectedError):
        return f"{exc.category.value}: {exc.detail}"
    return f"{type(exc).__name__}: {exc}"


@through_seam()
async def base_normalize_task(
    ctx: dict[str, Any],
    job_id: str,  # UUID as string (ARQ JSON serialization)
    project_base_id: str,  # UUID as string
) -> dict[str, Any]:
    """Normalize the project base referenced by ``project_base_id``.

    Wrapped by the L2 execution seam (:func:`through_seam`): the seam owns the
    ``Job.status`` lifecycle (active → complete on the returned dict / failed on
    raise) and the ProjectBase liveness check (a vanished / soft-deleted base →
    ``obsolete``, this body skipped). The body is pure domain — it drives the
    ``project_bases`` row through ``pending → ready | failed(reason)`` and
    returns the result dict the seam persists to ``Job.result_data``.

    See the module docstring for the permanent-vs-transient error contract.
    """
    log = logger.bind(job_id=job_id, project_base_id=project_base_id)
    s3: S3Client = ctx["s3_client"]
    session_factory = ctx["session_factory"]

    async with session_factory() as session:
        pb_repo = ProjectBaseRepository(session)

        base = await pb_repo.get_by_id(uuid.UUID(project_base_id))
        if base is None:
            # The seam turns a dead subject into `obsolete` before this body
            # runs; reaching here means an extreme race (the row vanished after
            # the seam's liveness check). Fail loudly rather than proceed.
            msg = "project_base vanished after the seam liveness check"
            raise NormalizerError(msg)

        archive_key = base.archive_key
        archive_kind = archive_kind_for_filename(archive_key)
        if archive_kind is None:
            reason = (
                f"unsupported_archive: cannot resolve archive kind from "
                f"key {archive_key!r}"
            )
            await pb_repo.mark_failed(base.id, failure_reason=reason)
            await session.commit()
            log.warning("base_normalize.unsupported_kind", archive_key=archive_key)
            raise NormalizerError(reason)

        # Fetch the raw archive. NoSuchKey → permanent (mark failed + raise →
        # seam `failed`); other ClientError (connection / throttle) → transient
        # → ``arq.Retry`` so ARQ actually re-runs (a bare re-raise does NOT retry
        # in ARQ — only Retry / RetryJob do — and would strand the Job `active`;
        # the seam passes ``arq.Retry`` through untouched).
        try:
            raw = await s3.get_object(archive_key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_ERROR_CODES:
                reason = f"archive_missing: {archive_key!r} not in storage ({code})"
                await pb_repo.mark_failed(base.id, failure_reason=reason)
                await session.commit()
                log.warning("base_normalize.archive_missing", error_code=code)
                raise NormalizerError(reason) from exc
            log.warning("base_normalize.transient_s3", error=str(exc))
            raise Retry(defer=_TRANSIENT_RETRY_DEFER_S) from exc

        # Deterministic normalization — the P1 sandbox enforces the structural
        # guards; content signals become excluded manifest rows.
        try:
            snapshot = normalize_archive(
                raw, archive_kind=archive_kind, limits=_PROJECT_NORMALIZE_LIMITS
            )
        except (NormalizerError, SecurityRejectedError) as exc:
            reason = _failure_reason(exc)
            await pb_repo.mark_failed(base.id, failure_reason=reason)
            await session.commit()
            log.warning("base_normalize.rejected", reason=reason)
            raise NormalizerError(reason) from exc

        # Persist the canonical snapshot, then flip pending → ready. The
        # snapshot_hash is the aggregate over the NORMALIZED content
        # (== manifest.aggregate_hash), NOT a hash of the raw zip bytes — that
        # determinism is what lets a student re-zip of the same content echo-
        # match this base in P3.
        snapshot_key = _snapshot_key_for(archive_key)
        await s3.upload_file(snapshot_key, snapshot.canonical_zip, "application/zip")
        await pb_repo.mark_ready(
            base.id,
            snapshot_key=snapshot_key,
            snapshot_hash=snapshot.snapshot_hash,
            manifest=manifest_to_jsonb(snapshot.manifest),
        )
        await session.commit()
        log.info(
            "base_normalize.ready",
            version=base.version,
            snapshot_hash=snapshot.snapshot_hash,
            included=len(snapshot.manifest.included),
            excluded=len(snapshot.manifest.excluded),
        )
        return {"state": "ready", "snapshot_hash": snapshot.snapshot_hash}
