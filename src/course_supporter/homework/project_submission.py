"""Project-submission processing for the homework worker (KD18 P3).

Runs inside ``arq_process_homework`` for a ``task_type='project'`` submission,
BEFORE the safety stage, and REPLACES the single-file path's
``extract_submission_content`` + ``run_stage1`` for that submission (which would
fail-close on a real project's non-allowlisted files — the same wall P2's base
worker hit, 1A). Deterministic, zero LLM. It:

1. normalizes the raw submission archive via the shared ``normalize_archive``
   (classify) with ``_PROJECT_NORMALIZE_LIMITS`` — a content/structural rejection
   fails the submission CLOSED (persisted like the stage-1 rejection);
2. reads every text file of the snapshot -- docx and pdf included -- through
   the same text screen a single file meets (task 11, decision 4): the
   encoding recovered, the signal screen run, the result in NFC. A file that
   cannot be read is named in ``not_opened`` instead of reaching the model
   as replacement characters; a file whose name says it may hold a secret
   never reached the snapshot at all (the normalizer excluded it) and is
   named here too; a direction override refuses the submission;
3. stores the canonical snapshot in S3 (a sibling of the raw key) + the three
   snapshot columns;
4. computes the base-vs-submission delta (derived on read from the two
   persisted manifests, not persisted here) and LOGS its counts;
5. builds the rich Mentor delta-context ``submission_text`` for safety →
   sanity → review via the pure :func:`build_mentor_context` (P4);
6. hands the evaluation stage the student's files -- new and changed against
   the base, as the screen read them (project chain K1) -- and, when the
   caller asks for it, the text of every file of the base, read as the
   student's files are, so a quote of the author's code can be told apart
   from the student's (chain K3). Today's Mentor reads neither: the text, the
   safety text and its review are what they were.

The context is the H2-budgeted trusted/untrusted delta assembly: a
system-computed trusted block (base tree + two-level delta + F2 metrics +
staleness) followed by priority-ordered untrusted file bodies / diffs. The
pure builder does the assembly; this worker supplies only the I/O — the base
snapshot zip, the texts the screen read, and a ``read_text`` closure over them.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from contextlib import ExitStack
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import structlog

from course_supporter.homework.doors import (
    DoorReading,
    not_opened_block,
    persist_door_refusal,
)
from course_supporter.homework.mentor_context import (
    ReadText,
    Side,
    build_mentor_context,
)
from course_supporter.homework.text_budget import project_context_budget_chars
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
from course_supporter.normalizer.models import EntryClass, ExcludedReason
from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)
from course_supporter.security.policies import HOMEWORK_POLICY
from course_supporter.security.schemas import NotOpenedEntry, ScreenFlag
from course_supporter.security.stage1 import archive_kind_for_filename
from course_supporter.security.text_screen import read_as_screened, screen_text
from course_supporter.storage.project_base_repository import ProjectBaseRepository

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.criteria_verdicts import WorkFile
    from course_supporter.normalizer import Delta
    from course_supporter.storage.homework_repository import HomeworkRepository
    from course_supporter.storage.orm import HomeworkSubmission, ProjectBase
    from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger(__name__)

# A project is the student's text: the mode their policy names (task 11).
_MODE: Final = HOMEWORK_POLICY.text_screen_mode

_NO_BASE: Final[Mapping[str, str]] = MappingProxyType({})
"""What :attr:`DoorReading.base_files` holds when the base is not read."""

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
    languages: Sequence[str] = (),
    base_corpus: bool = False,
) -> DoorReading | None:
    """Normalize a project submission, screen its texts, persist its snapshot,
    log the delta, and return what the doors read — the rich Mentor
    delta-context as ``text``, with ``not_opened`` and the screen's flags — or
    ``None`` on a fail-closed rejection (already persisted; the caller returns).

    ``languages`` verifies a recovered encoding, exactly as for a single file.

    ``base_corpus`` asks for the text of every file of the base in
    :attr:`DoorReading.base_files` -- the new path's evaluation, which tells a
    quote of the author's code from the student's (project chain K3). Read
    once, from the base snapshot the delta context was built from, and only
    for a submission that passed the size check. Today's Mentor does not ask:
    its reading is what it was.

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

    # Screen before anything is stored: a refused project leaves no snapshot.
    extractor = DefaultTextExtractor()
    try:
        screen = _screen_snapshot(snapshot.canonical_zip, snapshot.manifest, languages)
    except SecurityRejectedError as exc:
        await persist_door_refusal(session, hw_repo, sid, exc)
        log.warning("project_submission.screen_refused", category=exc.category.value)
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

    # Assemble the rich context inside a resource-scoped block. The submission's
    # texts were read by the screen above; only the base snapshot is fetched
    # from S3, and only when a base is attached. It closes once the pure builder
    # has read every body it needs (it reads lazily during assembly, entirely
    # within this ``with``).
    base_bytes: bytes | None = None
    with ExitStack() as stack:
        base_zf: zipfile.ZipFile | None = None
        if base is not None and base.snapshot_key is not None:
            base_bytes = await s3.get_object(base.snapshot_key)
            base_zf = stack.enter_context(zipfile.ZipFile(io.BytesIO(base_bytes)))

        def read_text(side: Side, entry: ManifestEntry) -> str | None:
            if side == "sub":
                # Only what the screen read: a file it set aside is None here,
                # exactly as a binary one is.
                return screen.texts.get(entry.path)
            if base_zf is None:
                return None
            return extractor.extract(entry.cls, base_zf.read(entry.path))

        context = build_mentor_context(
            base_manifest=base_manifest,
            sub_manifest=snapshot.manifest,
            delta=delta,
            read_text=read_text,
            base_version=base_version,
            latest_version=latest_version,
        )
        # After the context, and by the same reader: the text above is the one
        # it always was.
        files = _student_work(delta, snapshot.manifest, read_text)

    # Oversize guard (step E) -- the ONE size limit on this branch. The
    # assembled context is measured against the budget derived from the FIRST
    # rung of every stage the submission reaches (homework/text_budget.py).
    # Over it, the submission is refused HERE, before safety: the E run showed
    # the alternative, where the gate's first rung is called anyway, refuses,
    # and the ladder descends, so the student pays for a refusal that was
    # knowable from the character count. The assembly used to carry a second,
    # larger cap of its own; it could never fire before this one for any
    # alphabet, and step E removed it rather than keep a dead branch.
    # What was not read is said to the Mentor the way it is for an archive:
    # one block, after the work, so no review rests on a partial reading
    # unknowingly. Counted against the budget like everything else. Kept last:
    # the safety check reads the context without it (DoorReading.safety_text).
    context += not_opened_block(screen.not_opened)

    budget_chars = project_context_budget_chars()
    if len(context) > budget_chars:
        log.warning(
            "project_submission.over_budget",
            context_chars=len(context),
            budget_chars=budget_chars,
        )
        await _persist_rejection(
            session,
            hw_repo,
            sid,
            f"over_budget: assembled context is {len(context)} characters "
            f"against a budget of {budget_chars}",
            category=ErrorCategory.OVER_BUDGET.value,
            details=f"{_grouped(len(context))} / {_grouped(budget_chars)}",
        )
        return None

    # The author's base, for the evaluation's quotes (K3): only now, so a
    # submission refused for its size never pays for reading it, and from the
    # snapshot already fetched, so S3 is asked for it once.
    base_files = _NO_BASE
    if base_corpus and base_bytes is not None:
        base_files = _read_base_corpus(base_bytes, base_manifest, languages, log=log)

    return DoorReading(
        text=context,
        not_opened=screen.not_opened,
        flags=screen.flags,
        files=files,
        base_files=base_files,
    )


def _student_work(
    delta: Delta, sub_manifest: Manifest, read_text: ReadText
) -> tuple[WorkFile, ...]:
    """The student's files (K1): each new or changed against the base.

    An unchanged file is the author's, a deleted one is not in the work, and
    an excluded one never reached the snapshot. Each carries the text the
    screen read for the models, so a binary file and a file the screen set
    aside (named in ``not_opened``) have none and are left out. A document's
    lines are an artefact of extraction, as on the text path, so it has none.
    In order of path; without a base every file is new.
    """
    from course_supporter.homework.criteria_verdicts import WorkFile

    sub_by = {entry.path: entry for entry in sub_manifest.included}
    files: list[WorkFile] = []
    for path in sorted((*delta.changed, *delta.new)):
        entry = sub_by[path]
        text = read_text("sub", entry)
        if text is not None:
            files.append(WorkFile(path, text, entry.cls is not EntryClass.DOCUMENT))
    return tuple(files)


def _read_base_corpus(
    base_zip: bytes, manifest: Manifest, languages: Sequence[str], *, log: Any
) -> Mapping[str, str]:
    """The text of every file of the author's base, by path (K3).

    Every file, not only the ones the student changed: the author's code a
    student moved or renamed is in a "new" file of the work, and the delta
    cannot tell it from the student's own. Each is read as the student's
    files are -- the encoding recovered in ``languages``, a document's text
    extracted, NFC (:func:`read_as_screened`) -- so a quote reads the same
    on both sides. Not screened: the base never reaches a model from here.

    A binary file has no text, and a text whose encoding cannot be
    established is left out, as the student's would be. A base document the
    extractor cannot open is no reason to fail the student's submission: it
    is logged and left out.
    """
    extractor = DefaultTextExtractor()
    corpus: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(base_zip)) as zf:
        for entry in manifest.included:
            if entry.cls is EntryClass.BINARY:
                continue
            raw = zf.read(entry.path)
            content: bytes | str = raw
            if entry.cls is EntryClass.DOCUMENT:
                try:
                    content = extractor.extract(entry.cls, raw) or ""
                except Exception:
                    log.warning(
                        "project_submission.base_document_unread", path=entry.path
                    )
                    continue
            try:
                corpus[entry.path] = read_as_screened(
                    content, name=entry.path, languages=languages
                )
            except SecurityRejectedError:
                continue
    return MappingProxyType(corpus)


class _SnapshotScreen:
    """What the screen read out of a snapshot, and what it set aside."""

    def __init__(self) -> None:
        self.texts: dict[str, str] = {}
        self.not_opened: tuple[NotOpenedEntry, ...] = ()
        self.flags: tuple[ScreenFlag, ...] = ()


def _screen_snapshot(
    canonical_zip: bytes, manifest: Manifest, languages: Sequence[str]
) -> _SnapshotScreen:
    """Every text file of the snapshot through the one text screen (task 11).

    Eager and whole: every TEXT and DOCUMENT entry is read here, not only the
    ones the delta will show, so a direction override in any file refuses the
    submission and the flags describe the whole project. Every name is
    screened too -- the names reach the model in the tree and the file frames.

    Raises:
        SecurityRejectedError: ``SUSPICIOUS_UNICODE`` from any text or name.
    """
    result = _SnapshotScreen()
    not_opened: list[NotOpenedEntry] = []
    flags: list[ScreenFlag] = []
    extractor = DefaultTextExtractor()
    with zipfile.ZipFile(io.BytesIO(canonical_zip)) as zf:
        for entry in manifest.included:
            flags.extend(_name_flags(entry.path))
            if entry.cls is EntryClass.BINARY:
                continue
            raw = zf.read(entry.path)
            content: bytes | str
            if entry.cls is EntryClass.DOCUMENT:
                try:
                    content = extractor.extract(entry.cls, raw) or ""
                except Exception:
                    # A docx that is not one, a pdf PyMuPDF cannot open: named
                    # with the reason a single broken document is refused for,
                    # instead of failing the whole project.
                    not_opened.append(
                        NotOpenedEntry(
                            arcname=entry.path,
                            reason=ErrorCategory.MAGIC_MISMATCH,
                            size=entry.size,
                        )
                    )
                    continue
            else:
                content = raw
            try:
                screened = screen_text(
                    content, name=entry.path, mode=_MODE, languages=languages
                )
            except SecurityRejectedError as exc:
                if exc.category is not ErrorCategory.CHARSET_VIOLATION:
                    raise
                not_opened.append(
                    NotOpenedEntry(
                        arcname=entry.path,
                        reason=ErrorCategory.CHARSET_VIOLATION,
                        size=entry.size,
                    )
                )
                continue
            result.texts[entry.path] = screened.text
            flags.extend(screened.flags)
    for excluded in manifest.excluded:
        if excluded.reason is ExcludedReason.DENYLIST_DIR:
            continue
        flags.extend(_name_flags(excluded.path))
        if excluded.reason is ExcludedReason.MAY_CONTAIN_SECRETS:
            not_opened.append(
                NotOpenedEntry(
                    arcname=excluded.path,
                    reason=ErrorCategory.MAY_CONTAIN_SECRETS,
                    size=excluded.size,
                )
            )
    result.not_opened = tuple(sorted(not_opened, key=lambda e: e.arcname))
    result.flags = tuple(flags)
    return result


def _name_flags(path: str) -> tuple[ScreenFlag, ...]:
    return screen_text(path, name=path, mode=_MODE, where="name").flags


def _grouped(n: int) -> str:
    """Thousands-separated number for the curated ``details`` string.

    A plain space, not a comma: the portal renders this into a Ukrainian
    sentence and a comma would read as a decimal mark there.
    """
    return f"{n:,}".replace(",", " ")


async def _persist_rejection(
    session: AsyncSession,
    hw_repo: HomeworkRepository,
    sid: uuid.UUID,
    reason: str,
    *,
    category: str | None = None,
    details: str | None = None,
) -> None:
    """Fail-closed persistence: safety result + submission rejected + commit.

    The Job → complete transition is the execution seam's: the caller returns
    None, the homework body returns, and the seam terminalises the Job.

    ``category`` and ``details`` are the caller-facing pair the portal needs:
    without a category ``curated_rejection`` returns None for this source and
    the interface falls back to the bare status phrase (DD-6-Z). The structural
    rejections above still pass neither -- their reasons carry library
    vocabulary and have no phrase in the portal dictionary yet, so the debt is
    closed only for the one category that does.
    """
    safety: dict[str, Any] = {"source": "normalizer", "reason": reason}
    if category is not None:
        safety["category"] = category
    if details is not None:
        safety["details"] = details
    await hw_repo.store_safety_result(sid, safety)
    await hw_repo.update_status(sid, "rejected", error_message=reason)
    await session.commit()
