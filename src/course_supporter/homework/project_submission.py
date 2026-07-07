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
3. computes the base-vs-submission delta and LOGS its counts — the delta is
   DERIVED on read (P4/P5) from the two persisted manifests, not persisted here;
4. builds a BOUNDED interim ``submission_text`` for safety → sanity → review.

The interim text is over-inclusive (the whole submission, not just the delta)
and byte-budgeted so it cannot blow up the LLM context — a stop-gap. P4 replaces
it at the ``tasks.py`` seam with the H2-budgeted trusted/untrusted delta context.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

import structlog

from course_supporter.normalizer import (
    _PROJECT_NORMALIZE_LIMITS,
    DefaultTextExtractor,
    EntryClass,
    Manifest,
    NormalizedSnapshot,
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
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.orm import HomeworkSubmission
    from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger(__name__)

# Byte budget for the interim ``submission_text`` fed to safety → sanity →
# review (Edit II). Kept at the 1 MB order of the existing single-file safety
# input (HOMEWORK_POLICY caps single files at 1 MB); the project path bypasses
# that policy, so without this an unbounded project could hand the LLM tens of MB.
PROJECT_SAFETY_TEXT_MAX_BYTES: Final[int] = 1024 * 1024

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


def build_interim_submission_text(snapshot: NormalizedSnapshot) -> str:
    """Bounded, over-inclusive interim text for the LLM stages (Edit II).

    Delimited concat (``--- {path} ---\\n{text}``, mirroring the single-file
    archive assembly) of every INCLUDED TEXT / DOCUMENT entry — docx / pdf are
    extracted here too via :class:`DefaultTextExtractor`; BINARY entries are
    outside the review scope and skipped. Whole entries are added until the next
    would exceed :data:`PROJECT_SAFETY_TEXT_MAX_BYTES`; the remainder is dropped
    and summarised in ONE aggregate omission marker. If the very first entry
    already exceeds the budget it is truncated (so the LLM is never handed an
    empty body). This is the whole submission, NOT the delta — P4 narrows it.
    """
    extractor = DefaultTextExtractor()
    parts: list[str] = []
    used = 0
    omitted_entries = 0
    omitted_bytes = 0
    budget_hit = False

    with zipfile.ZipFile(io.BytesIO(snapshot.canonical_zip)) as zf:
        for entry in snapshot.manifest.included:
            if entry.cls not in (EntryClass.TEXT, EntryClass.DOCUMENT):
                continue  # BINARY — hash-tracked only, outside review scope.
            if budget_hit:
                omitted_entries += 1
                omitted_bytes += entry.size
                continue
            text = extractor.extract(entry.cls, zf.read(entry.path))
            if text is None:
                continue
            block = f"--- {entry.path} ---\n{text}"
            block_bytes = len(block.encode("utf-8"))
            sep = 1 if parts else 0
            if used + sep + block_bytes > PROJECT_SAFETY_TEXT_MAX_BYTES:
                if not parts:
                    # First eligible block over budget — include a truncated
                    # slice so safety is never blind, then stop.
                    truncated = block.encode("utf-8")[:PROJECT_SAFETY_TEXT_MAX_BYTES]
                    parts.append(truncated.decode("utf-8", errors="ignore"))
                    used = len(truncated)
                    budget_hit = True
                    continue
                budget_hit = True
                omitted_entries += 1
                omitted_bytes += entry.size
                continue
            parts.append(block)
            used += sep + block_bytes

    text = "\n".join(parts)
    if omitted_entries:
        text += (
            f"\n--- omitted for budget: {omitted_entries} entries, "
            f"{omitted_bytes} bytes ---"
        )
    return text


async def process_project_submission(
    *,
    session: AsyncSession,
    s3: S3Client,
    hw_repo: HomeworkRepository,
    job_repo: JobRepository,
    submission: HomeworkSubmission,
    sid: uuid.UUID,
    jid: uuid.UUID,
    file_bytes: bytes,
    raw_key: str,
) -> str | None:
    """Normalize a project submission, persist its snapshot, log the delta, and
    return the bounded interim ``submission_text`` — or ``None`` on a fail-closed
    rejection (already persisted; the caller returns).

    Fail-closed on a content/structural rejection (a malformed / bomb archive):
    persist a ``{"source": "normalizer", "reason": ...}`` safety result, set the
    submission ``rejected`` + the Job ``complete``, commit, and return ``None`` —
    mirroring both the base worker's ``mark_failed`` and the single-file stage-1
    rejection, so no ARQ retry and no crash.
    """
    log = logger.bind(submission_id=str(sid), job_id=str(jid))

    archive_kind = archive_kind_for_filename(submission.original_filename or "")
    if archive_kind is None:
        # The preflight guarantees an archive filename; treat an unresolvable
        # kind here as a clean rejection rather than a crash.
        reason = "unsupported_archive: cannot resolve archive kind"
        await _persist_rejection(session, hw_repo, job_repo, sid, jid, reason)
        log.warning("project_submission.unsupported_kind")
        return None

    try:
        snapshot = normalize_archive(
            file_bytes, archive_kind=archive_kind, limits=_PROJECT_NORMALIZE_LIMITS
        )
    except (NormalizerError, SecurityRejectedError) as exc:
        reason = _project_failure_reason(exc)
        await _persist_rejection(session, hw_repo, job_repo, sid, jid, reason)
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

    # Delta — LOGGED only. It is derived on read (P4/P5) from the two persisted
    # manifests, so nothing is persisted here and nothing reaches the Mentor yet.
    base_manifest = _EMPTY_MANIFEST
    if submission.base_id is not None:
        base = await ProjectBaseRepository(session).get_by_id(submission.base_id)
        if base is not None and base.manifest is not None:
            base_manifest = manifest_from_jsonb(base.manifest)
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

    return build_interim_submission_text(snapshot)


async def _persist_rejection(
    session: AsyncSession,
    hw_repo: HomeworkRepository,
    job_repo: JobRepository,
    sid: uuid.UUID,
    jid: uuid.UUID,
    reason: str,
) -> None:
    """Fail-closed persistence: safety result + rejected + Job complete + commit."""
    await hw_repo.store_safety_result(sid, {"source": "normalizer", "reason": reason})
    await hw_repo.update_status(sid, "rejected", error_message=reason)
    await job_repo.update_status(jid, "complete")
    await session.commit()
