"""Base-archive normalization ARQ worker (KD18 P2).

Deterministic, zero LLM, no ``ExternalServiceCall``. Consumes the P1
normalizer library and drives the ``project_bases`` row through
``pending → ready | failed(reason)``.

Payload (from :func:`course_supporter.enqueue.enqueue_base_normalize`):
``(job_id, project_base_id)``. The ``archive_key`` is re-read from the
``ProjectBase`` row — the payload carries only ids. On success the canonical
snapshot lands in S3 and the row flips to ``ready`` with the aggregate
``snapshot_hash`` (the P3 echo-match key) + the algorithmic manifest.

Error discipline (mirrors ``s3_cleanup_task``):

* **Permanent** — a content/structural rejection (``NormalizerError`` /
  ``SecurityRejectedError``), an unresolvable archive kind, or a missing
  archive (S3 ``NoSuchKey``): flip the row to ``failed(reason)`` + the Job to
  ``failed`` and RETURN (swallowed — no ARQ retry would ever succeed).
* **Transient** — a connection-level S3 error (not ``NoSuchKey``): propagate
  so ARQ retries (``max_tries``).

Security note: under KD18 1A the author upload path does NOT run
``run_stage1`` (its archive branch is fail-closed and would reject a real
project's non-allowlisted files). The MANDATORY structural sandbox for a base
is ``extract_archive_safely`` in classify mode, reached via ``normalize_archive``
HERE. The resource guards therefore come from ``_BASE_NORMALIZE_LIMITS`` below,
NOT from a ``ContextPolicy`` (``AUTHORED_POLICY`` leaves archives disabled).
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath
from typing import Any, Final

import structlog
from botocore.exceptions import ClientError

from course_supporter.normalizer import (
    NormalizerError,
    NormalizerLimits,
    manifest_to_jsonb,
    normalize_archive,
)
from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.security.stage1 import archive_kind_for_filename
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger(__name__)

# ── Base normalization limits (SECURITY KNOB — review-able) ────────────
# Explicit resource guards for base-archive normalization. Distinct from the
# route's raw-upload size cap (100 MB compressed): these bound the *unpack*.
# The P1 ``NormalizerLimits`` API exposes exactly these four knobs — there is
# no separate per-entry-size or entry-count param; the anti-zip-bomb defence is
# the cumulative ``raw_max_unzipped_bytes`` + ``raw_max_nesting_depth``, and the
# entry count is bounded implicitly by the cumulative unzipped size.
#   raw_max_unzipped_bytes 200 MB — level-1 cumulative unzip guard (anti-bomb),
#       generous for a real author-curated project, far below any bomb.
#   raw_max_nesting_depth   1     — a nested archive is recorded as an excluded
#       entry, never recursed into (bomb vector stays unreachable).
#   kept_total_max_bytes  100 MB  — level-2 cap on the CLEANED snapshot, applied
#       AFTER the denylist collapse (a forgotten .venv does not count).
#   kept_single_max_bytes   5 MB  — P4 per-file review threshold; the normalizer
#       stores but does not enforce it (inert in P2).
_BASE_NORMALIZE_LIMITS: Final[NormalizerLimits] = NormalizerLimits(
    raw_max_unzipped_bytes=200 * 1024 * 1024,
    raw_max_nesting_depth=1,
    kept_total_max_bytes=100 * 1024 * 1024,
    kept_single_max_bytes=5 * 1024 * 1024,
)

# S3 "object does not exist" codes — a missing archive is PERMANENT (the raw
# upload vanished), not a retry-able connection error.
_NOT_FOUND_ERROR_CODES: frozenset[str] = frozenset({"NoSuchKey", "404", "NotFound"})


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


async def base_normalize_task(
    ctx: dict[str, Any],
    job_id: str,  # UUID as string (ARQ JSON serialization)
    project_base_id: str,  # UUID as string
) -> dict[str, Any]:
    """Normalize the project base referenced by ``project_base_id``.

    See module docstring for the full error-handling contract. Returns a small
    result dict (``{"state": ..., ...}``) on every terminal outcome; raises only
    on a transient S3 error (for ARQ retry).
    """
    log = logger.bind(job_id=job_id, project_base_id=project_base_id)
    s3: S3Client = ctx["s3_client"]
    session_factory = ctx["session_factory"]

    async with session_factory() as session:
        job_repo = JobRepository(session)
        pb_repo = ProjectBaseRepository(session)

        base = await pb_repo.get_by_id(uuid.UUID(project_base_id))
        if base is None:
            # The row vanished (parent document deleted) — nothing to do.
            log.warning("base_normalize.base_missing")
            await job_repo.update_status(
                uuid.UUID(job_id), "failed", error_message="project_base not found"
            )
            await session.commit()
            return {"state": "failed", "reason": "base_missing"}

        # Job → active, committed early so the reconciler sees an orphaned
        # active job if this worker dies mid-normalize.
        await job_repo.update_status(uuid.UUID(job_id), "active")
        await session.commit()

        archive_key = base.archive_key
        archive_kind = archive_kind_for_filename(archive_key)
        if archive_kind is None:
            reason = (
                f"unsupported_archive: cannot resolve archive kind from "
                f"key {archive_key!r}"
            )
            await pb_repo.mark_failed(base.id, failure_reason=reason)
            await job_repo.update_status(
                uuid.UUID(job_id), "failed", error_message=reason
            )
            await session.commit()
            log.warning("base_normalize.unsupported_kind", archive_key=archive_key)
            return {"state": "failed", "reason": reason}

        # Fetch the raw archive. NoSuchKey → permanent; other ClientError
        # (connection / throttle) → propagate for ARQ retry.
        try:
            raw = await s3.get_object(archive_key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_ERROR_CODES:
                reason = f"archive_missing: {archive_key!r} not in storage ({code})"
                await pb_repo.mark_failed(base.id, failure_reason=reason)
                await job_repo.update_status(
                    uuid.UUID(job_id), "failed", error_message=reason
                )
                await session.commit()
                log.warning("base_normalize.archive_missing", error_code=code)
                return {"state": "failed", "reason": reason}
            raise

        # Deterministic normalization — the P1 sandbox enforces the structural
        # guards; content signals become excluded manifest rows.
        try:
            snapshot = normalize_archive(
                raw, archive_kind=archive_kind, limits=_BASE_NORMALIZE_LIMITS
            )
        except (NormalizerError, SecurityRejectedError) as exc:
            reason = _failure_reason(exc)
            await pb_repo.mark_failed(base.id, failure_reason=reason)
            await job_repo.update_status(
                uuid.UUID(job_id), "failed", error_message=reason
            )
            await session.commit()
            log.warning("base_normalize.rejected", reason=reason)
            return {"state": "failed", "reason": reason}

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
        await job_repo.update_status(uuid.UUID(job_id), "complete")
        await session.commit()
        log.info(
            "base_normalize.ready",
            version=base.version,
            snapshot_hash=snapshot.snapshot_hash,
            included=len(snapshot.manifest.included),
            excluded=len(snapshot.manifest.excluded),
        )
        return {"state": "ready", "snapshot_hash": snapshot.snapshot_hash}
