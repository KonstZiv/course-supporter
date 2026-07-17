"""Project-submission processing for the homework worker (KD18 P3).

Runs inside ``arq_process_homework`` for a ``task_type='project'`` submission,
BEFORE the safety stage, and REPLACES the single-file path's
``extract_submission_content`` + ``run_stage1`` for that submission (which would
fail-close on a real project's non-allowlisted files — the same wall P2's base
worker hit, 1A). Deterministic, zero LLM. It:

1. normalizes the raw submission archive via the shared ``normalize_archive``
   (classify) with ``_PROJECT_NORMALIZE_LIMITS`` — a content/structural rejection
   fails the submission CLOSED (persisted like the stage-1 rejection);
2. stores the canonical snapshot in S3 (a sibling of the raw key) + the three
   snapshot columns;
3. computes the base-vs-submission delta (derived on read from the two
   persisted manifests, not persisted here) and LOGS its counts;
4. builds the rich Mentor delta-context ``submission_text`` for safety →
   sanity → review via the pure :func:`build_mentor_context` (P4).

The context is the H2-budgeted trusted/untrusted delta assembly: a
system-computed trusted block (base tree + two-level delta + F2 metrics +
staleness) followed by priority-ordered untrusted file bodies / diffs. The
pure builder does the assembly; this worker supplies only the I/O — the two
snapshot zips and a ``read_text`` closure over them.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from contextlib import ExitStack
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

import structlog

from course_supporter.homework.mentor_context import Side, build_mentor_context
from course_supporter.normalizer import (
    _PROJECT_NORMALIZE_LIMITS,
    DefaultTextExtractor,
    Manifest,
    ManifestEntry,
    NormalizerError,
    compute_delta,
    manifest_from_jsonb,
    manifest_to_jsonb,
    normalize_archive,
)
from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.security.stage1 import archive_kind_for_filename
from course_supporter.storage.project_base_repository import ProjectBaseRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.homework_repository import HomeworkRepository
    from course_supporter.storage.orm import HomeworkSubmission, ProjectBase
    from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger(__name__)

# An empty base manifest — the "no base attached" case. compute_delta against it
# yields every submission path as "new" (KD18: base absent → delta "all new").
_EMPTY_MANIFEST: Final[Manifest] = Manifest(
    schema=1,
    aggregate_hash="",
    included=(),
    excluded=(),
    total_files=0,
    total_bytes=0,
)


def _submission_snapshot_key(raw_key: str) -> str:
    """Sibling snapshot key: ``homework/{t}/{sid}/x.zip`` → ``.../snapshot.zip``.

    Mirrors the base worker's ``_snapshot_key_for`` — the normalized snapshot is
    the second key of the submission (raw + snapshot).
    """
    return str(PurePosixPath(raw_key).parent / "snapshot.zip")


def _project_failure_reason(exc: NormalizerError | SecurityRejectedError) -> str:
    """Human-readable rejection reason (mirror of the base worker's helper).

    A ``SecurityRejectedError`` is prefixed with its security ``category``; a
    ``NormalizerError`` (no category) carries its class name + message.
    """
    if isinstance(exc, SecurityRejectedError):
        return f"{exc.category.value}: {exc.detail}"
    return f"{type(exc).__name__}: {exc}"


async def process_project_submission(
    *,
    session: AsyncSession,
    s3: S3Client,
    hw_repo: HomeworkRepository,
    submission: HomeworkSubmission,
    sid: uuid.UUID,
    jid: uuid.UUID,
    file_bytes: bytes,
    raw_key: str,
) -> str | None:
    """Normalize a project submission, persist its snapshot, log the delta, and
    return the rich Mentor delta-context ``submission_text`` — or ``None`` on a
    fail-closed rejection (already persisted; the caller returns).

    Fail-closed on a content/structural rejection (a malformed / bomb archive):
    persist a ``{"source": "normalizer", "reason": ...}`` safety result, set the
    submission ``rejected``, commit, and return ``None`` — the caller returns and
    the L2 execution seam writes the Job ``complete`` (no ARQ retry, no crash).
    """
    log = logger.bind(submission_id=str(sid), job_id=str(jid))

    archive_kind = archive_kind_for_filename(submission.original_filename or "")
    if archive_kind is None:
        # The preflight guarantees an archive filename; treat an unresolvable
        # kind here as a clean rejection rather than a crash.
        reason = "unsupported_archive: cannot resolve archive kind"
        await _persist_rejection(session, hw_repo, sid, reason)
        log.warning("project_submission.unsupported_kind")
        return None

    try:
        snapshot = normalize_archive(
            file_bytes, archive_kind=archive_kind, limits=_PROJECT_NORMALIZE_LIMITS
        )
    except (NormalizerError, SecurityRejectedError) as exc:
        reason = _project_failure_reason(exc)
        await _persist_rejection(session, hw_repo, sid, reason)
        log.warning("project_submission.rejected", reason=reason)
        return None

    # Persist the canonical snapshot (S3 sibling of the raw key) + the columns.
    snapshot_key = _submission_snapshot_key(raw_key)
    await s3.upload_file(snapshot_key, snapshot.canonical_zip, "application/zip")
    await hw_repo.store_snapshot(
        sid,
        snapshot_key=snapshot_key,
        snapshot_hash=snapshot.snapshot_hash,
        snapshot_manifest=manifest_to_jsonb(snapshot.manifest),
    )
    await session.commit()

    # Resolve the base once: its manifest drives the delta (derived on read,
    # never persisted) and its version drives the staleness line. base_id is set
    # (preflight) only for a matched READY base, so its snapshot_key / manifest /
    # version are all populated; base absent → empty manifest → "all new".
    base_manifest = _EMPTY_MANIFEST
    base: ProjectBase | None = None
    base_version: int | None = None
    latest_version: int | None = None
    if submission.base_id is not None:
        repo = ProjectBaseRepository(session)
        base = await repo.get_by_id(submission.base_id)
        if base is not None:
            base_version = base.version
            if base.manifest is not None:
                base_manifest = manifest_from_jsonb(base.manifest)
        latest = await repo.get_latest_ready(submission.authored_document_id)
        latest_version = latest.version if latest is not None else None

    delta = compute_delta(base_manifest, snapshot.manifest)
    log.info(
        "project_submission.delta",
        changed=len(delta.changed),
        new=len(delta.new),
        deleted=len(delta.deleted),
        hygiene_new_excluded=len(delta.hygiene_new_excluded),
        base_id=str(submission.base_id) if submission.base_id else None,
        snapshot_hash=snapshot.snapshot_hash,
    )

    # Assemble the rich context inside a resource-scoped block. The submission
    # snapshot is already in memory (just uploaded); only the base snapshot is
    # fetched from S3, and only when a base is attached. Both zips close once the
    # pure builder has read every body it needs (it reads lazily during assembly,
    # entirely within this ``with``).
    extractor = DefaultTextExtractor()
    with ExitStack() as stack:
        sub_zf = stack.enter_context(
            zipfile.ZipFile(io.BytesIO(snapshot.canonical_zip))
        )
        base_zf: zipfile.ZipFile | None = None
        if base is not None and base.snapshot_key is not None:
            base_bytes = await s3.get_object(base.snapshot_key)
            base_zf = stack.enter_context(zipfile.ZipFile(io.BytesIO(base_bytes)))

        def read_text(side: Side, entry: ManifestEntry) -> str | None:
            zf = sub_zf if side == "sub" else base_zf
            if zf is None:
                return None
            return extractor.extract(entry.cls, zf.read(entry.path))

        return build_mentor_context(
            base_manifest=base_manifest,
            sub_manifest=snapshot.manifest,
            delta=delta,
            read_text=read_text,
            base_version=base_version,
            latest_version=latest_version,
        )


async def _persist_rejection(
    session: AsyncSession,
    hw_repo: HomeworkRepository,
    sid: uuid.UUID,
    reason: str,
) -> None:
    """Fail-closed persistence: safety result + submission rejected + commit.

    The Job → complete transition is the execution seam's: the caller returns
    None, the homework body returns, and the seam terminalises the Job.
    """
    await hw_repo.store_safety_result(sid, {"source": "normalizer", "reason": reason})
    await hw_repo.update_status(sid, "rejected", error_message=reason)
    await session.commit()
