"""Safe archive extraction for KD14 Stage 1 (KD-A).

Wraps stdlib ``zipfile`` / ``tarfile`` with multi-layer attack defense.
Reads in chunks against a shared byte budget; never delegates to
``extractall`` (which would happily honor path traversal); validates
each entry syntactically and structurally before yielding.

## Defense layers

1. **Path traversal** -- arcname syntactic validation: empty, null
   byte, backslash, leading ``/``, ``..`` segments all raise
   ``ARCHIVE_VIOLATION``. ``zipfile.extractall`` /
   ``tarfile.extractall`` are explicitly not used; iteration is
   manual per entry.

2. **Bombs (memory)** -- four checks per archive_kind:

   * Pre-extract: ``sum(declared_size) > max_unzipped_size`` rejects.
   * Per-entry size cap = ``max_unzipped_size // 2`` rejects asymmetric
     bombs where one entry dominates the budget. Applied to both
     archive_kind for symmetric defense.
   * ZIP per-entry compression ratio ``> 100x`` rejects.
   * tar.gz total compression ratio ``= total_uncompressed / archive_bytes``
     rejects ``> 100x`` (gzip wraps the whole tar; per-entry ratio
     is not meaningful).

3. **Bombs (CPU-bound decompression timing)** -- *not addressed*. An
   adversary could craft a tar.gz that takes long to decompress
   without exceeding the size budget. Mitigated by overall HTTP
   request timeout enforced upstream by FastAPI / nginx, not by this
   module.

4. **Symlink / hard-link / device / FIFO** -- TAR rejects any
   ``member.type`` other than ``REGTYPE`` / ``AREGTYPE`` / ``DIRTYPE``.
   ZIP defensively rejects entries whose Unix-mode bits in
   ``external_attr`` indicate symlink / socket / device / FIFO.

5. **Nesting depth** -- two limits:

   * Directory depth within a single archive: ``arcname.count("/")
     > _MAX_DIRECTORY_DEPTH`` (16) rejects (DoS/sanity guard, not a
     security boundary; applied only to entries that survive the
     skip gate, see layer 9).
   * Archive-within-archive recursion: caller-supplied
     ``max_nesting_depth``. Semantics: ``current_depth >=
     max_nesting_depth`` raises. With ``max_nesting_depth=3`` per
     KD14 homework -- top-level archive plus up to 2 levels of
     nested archives are processed (3 total levels). The fourth
     level's recursion call is rejected before any of its entries
     are read.

6. **Filename encoding** -- ``zipfile`` auto-detects UTF-8 via the
   flag bit (0x800), CP437 fallback otherwise. The decoded arcname
   is then NFKC-normalized via the security layer's
   ``normalize_filename`` so look-alike attacks against the
   downstream extension whitelist resolve deterministically.

7. **Recursive whitelist propagation** -- entries whose arcname
   ends with a recognised archive suffix (``.zip``, ``.tar.gz``,
   ``.tgz``) are extracted recursively; their decompressed contents
   are revalidated against the same ``allowed_extensions`` set and
   the same shared byte budget. Non-archive entries pass
   ``allowed_extensions`` membership check then
   ``verify_extension_matches_content`` (extension matches content).

8. **PAX header validation** -- *deferred*. Modern tar PAX-header
   attacks are rare; MVP ships without explicit PAX vetting. See
   POST-MR-NOTES forward-looking note.

9. **Denylist skip (№14 jurisdiction split)** -- the member loop
   separates HOSTILITY (traversal / symlink / device: whole-archive
   reject, even for entries under a skip prefix -- an archive
   showing attack markers deserves no trust) from UNTIDINESS
   (denylist prefixes via the caller-supplied ``skip_matcher``:
   the entry is dropped BEFORE resource accounting -- never read,
   never counted toward directory depth, per-entry caps,
   compression ratio, or the declared-size pre-check). Per-member
   check order: hostility -> skip -> accounting -> extraction.
   Skipping tightens rather than weakens the bomb defense: the best
   protection against a bomb inside ``node_modules`` is to never
   decompress it at all.
"""

from __future__ import annotations

import gzip
import io
import stat
import tarfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import IO, Literal, overload

import anyio
import structlog

from course_supporter.config import get_settings
from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)
from course_supporter.security.file_type import (
    extension_of,
    verify_extension_matches_content,
)
from course_supporter.security.normalization import normalize_filename
from course_supporter.security.schemas import (
    FileContent,
    SecurityWarning,
    SubmissionContent,
)

logger = structlog.get_logger()

# Hardcoded directory-depth limit. A sanity/DoS guard, NOT a security
# boundary -- bombs are stopped by the byte budget / compression-ratio /
# per-entry caps. Honest Java packages and monorepos reach 10-12
# levels; 16 leaves headroom now that denylist junk is skipped before
# accounting (№14: raised from 8 after a real lesson archive was
# rejected because __MACOSX/ over .angular/cache pushed it to depth 9).
_MAX_DIRECTORY_DEPTH = 16

# A skip matcher decides, per NFKC-normalized arcname, whether the entry
# sits under a denylisted prefix (untidiness jurisdiction, №14): it
# returns the matched prefix, or None to process the entry normally.
# The canonical production matcher is the normalizer's
# ``denylist_prefix`` -- injected by callers so the security layer keeps
# zero upward imports.
SkipMatcher = Callable[[str], str | None]

# Compression-ratio threshold above which an archive entry (ZIP) or
# the whole archive (tar.gz) is treated as a bomb.
_COMPRESSION_RATIO_LIMIT = 100

# Streaming chunk size for content extraction.
_CHUNK_SIZE = 64 * 1024

# Unix-mode bit positions for ZIP external_attr (creator system 3 == Unix).
_ZIP_MODE_MASK = 0xF000
_ZIP_MODE_REGULAR = 0x8000
_ZIP_MODE_DIRECTORY = 0x4000


@dataclass(frozen=True, slots=True)
class ExtractedFile:
    """A single validated file yielded from an archive.

    Attributes:
        arcname: NFKC-normalized, path-validated archive entry name.
            Safe to pass to downstream filesystem operations -- no
            absolute paths, no ``..`` segments, no backslash, no
            null bytes, no directory depth beyond
            :data:`_MAX_DIRECTORY_DEPTH`.
        content: Full decompressed bytes. Memory bound by the
            ``max_unzipped_size`` budget passed to
            :func:`extract_archive_safely`.
        depth: Archive-recursion depth at which this entry was
            extracted. ``0`` for entries inside the top-level
            archive; increments on each level of nested archive.
    """

    arcname: str
    content: bytes
    depth: int


class EntryVerdict(StrEnum):
    """Per-entry content-gate outcome, surfaced only in classify mode.

    In the default (strict) mode the content gate raises instead of
    yielding a verdict (see :func:`extract_archive_safely`). The
    structural guards (traversal, bomb, symlink, declared-size) are
    orthogonal -- they raise in both modes and never produce a
    verdict.
    """

    INCLUDED = "included"
    """Extension is whitelisted and content matches the extension."""
    FORBIDDEN_TYPE = "forbidden_type"
    """Extension is not in ``allowed_extensions`` (strict mode raises
    ``FORBIDDEN_TYPE`` here instead)."""
    MAGIC_MISMATCH = "magic_mismatch"
    """Whitelisted extension but content disagrees / is empty (strict
    mode raises ``MAGIC_MISMATCH`` here instead)."""
    NESTED_ARCHIVE = "nested_archive"
    """Entry is itself an archive; in classify mode it is NOT opened
    (the nested-archive bomb vector stays unreachable) -- strict mode
    recurses into it instead."""
    DENYLIST_SKIP = "denylist_skip"
    """Entry sits under a caller-supplied skip prefix (untidiness
    jurisdiction, №14). It is never opened: ``content`` is empty and
    ``declared_size`` carries the header-declared size. Strict mode
    silently drops the entry instead of yielding."""


@dataclass(frozen=True, slots=True)
class ClassifiedEntry:
    """Structurally-valid archive entry annotated with its verdict.

    Yielded only by ``extract_archive_safely(classify=True)``. Mirrors
    :class:`ExtractedFile` (same ``arcname`` / ``content`` / ``depth``
    guarantees) and adds :attr:`verdict`. The normalizer (KD18)
    consumes the full annotated stream to build a manifest that covers
    excluded content, rather than aborting on the first
    non-whitelisted entry.

    Attributes:
        arcname: NFKC-normalized, path-validated archive entry name.
        content: Full decompressed bytes. For ``NESTED_ARCHIVE`` this
            is the nested archive's own raw bytes -- it is never
            opened, so the caller hashes it as an opaque blob. For
            ``DENYLIST_SKIP`` it is always empty (the entry is never
            read).
        depth: Archive-recursion depth. Always the top level in
            classify mode, since nested archives are not opened.
        verdict: The content-gate outcome (:class:`EntryVerdict`).
        declared_size: Uncompressed size in bytes. Equals
            ``len(content)`` for every read verdict; for
            ``DENYLIST_SKIP`` the entry is never read, so this is the
            archive-header-declared size -- trusted for
            manifest/structure reporting only, never for budget
            accounting.
    """

    arcname: str
    content: bytes
    depth: int
    verdict: EntryVerdict
    declared_size: int


class _Budget:
    """Mutable byte counter shared across recursive extraction.

    Raises :class:`SecurityRejectedError` (``ARCHIVE_VIOLATION``)
    when cumulative consumption exceeds the configured limit.
    Intentionally not exposed in the public API; callers observe
    overruns through the exception, not direct counter inspection.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0

    def consume(self, n: int) -> None:
        self._used += n
        if self._used > self._limit:
            raise SecurityRejectedError(
                ErrorCategory.ARCHIVE_VIOLATION,
                f"unzipped size exceeds budget {self._limit} bytes",
            )


@overload
def extract_archive_safely(
    archive_bytes: bytes,
    *,
    archive_kind: Literal["zip", "tar.gz"],
    max_unzipped_size: int,
    max_nesting_depth: int,
    allowed_extensions: frozenset[str],
    classify: Literal[False] = False,
    skip_matcher: SkipMatcher | None = None,
) -> Iterator[ExtractedFile]: ...


@overload
def extract_archive_safely(
    archive_bytes: bytes,
    *,
    archive_kind: Literal["zip", "tar.gz"],
    max_unzipped_size: int,
    max_nesting_depth: int,
    allowed_extensions: frozenset[str],
    classify: Literal[True],
    skip_matcher: SkipMatcher | None = None,
) -> Iterator[ClassifiedEntry]: ...


def extract_archive_safely(
    archive_bytes: bytes,
    *,
    archive_kind: Literal["zip", "tar.gz"],
    max_unzipped_size: int,
    max_nesting_depth: int,
    allowed_extensions: frozenset[str],
    classify: bool = False,
    skip_matcher: SkipMatcher | None = None,
) -> Iterator[ExtractedFile | ClassifiedEntry]:
    """Iterate validated files from an archive; raise on first violation.

    Args:
        archive_bytes: Raw archive bytes (already validated against
            magic-bytes / size limits at Stage 1 entry).
        archive_kind: ``"zip"`` or ``"tar.gz"`` -- caller must
            specify; this function does not autodetect.
        max_unzipped_size: Cumulative byte budget for ALL extracted
            content across nested archives. Per-entry hard cap is
            ``max_unzipped_size // 2`` (asymmetric-bomb defense).
        max_nesting_depth: Maximum total levels of archive nesting
            allowed. ``current_depth >= max_nesting_depth`` raises.
            For homework context per vision §KD14, value is 3 --
            meaning top-level archive plus up to 2 levels of nested
            archives are processed.
        allowed_extensions: Lower-cased extension whitelist applied
            to every non-archive entry. Archive extensions
            (``zip`` / ``gz`` / ``tgz``) are recognised structurally
            and recursed regardless of membership; the contents
            inside them must satisfy this whitelist.
        skip_matcher: Optional per-entry skip gate (№14 jurisdiction
            split). Called with the NFKC-normalized arcname; a
            non-None return (the matched denylist prefix) drops the
            entry BEFORE resource accounting: it is never read and
            does not count toward directory depth, per-entry caps,
            compression ratio, or the declared-size pre-check.
            Hostility checks still run first and reject the whole
            archive even inside the skip zone. In classify mode a
            skipped entry surfaces as a ``DENYLIST_SKIP`` verdict
            carrying ``declared_size``; in strict mode it is silently
            dropped. The canonical production matcher is the
            normalizer's ``denylist_prefix``, injected by the caller
            so this module keeps zero upward imports.

    Yields:
        :class:`ExtractedFile` per non-archive, non-directory entry,
        in archive iteration order. Recursive calls flatten nested
        archive contents into the same iterator.

    Raises:
        SecurityRejectedError: with ``ARCHIVE_VIOLATION`` on any
            structural violation (path traversal, bomb, symlink,
            depth limit, encoding); ``FORBIDDEN_TYPE`` on whitelist
            failure for a file inside the archive;
            ``MAGIC_MISMATCH`` on extension/content disagreement
            for a non-archive entry. In classify mode the three
            content-level signals are demoted to annotated yields
            (see below); the structural ``ARCHIVE_VIOLATION`` guards
            still raise.

    Classify mode (``classify=True``, KD18 P1 KD-A): yields
    :class:`ClassifiedEntry` instead of :class:`ExtractedFile`.
    Non-whitelisted extensions, magic mismatches, and nested archives
    no longer raise / recurse -- they surface as ``FORBIDDEN_TYPE`` /
    ``MAGIC_MISMATCH`` / ``NESTED_ARCHIVE`` verdicts so the caller
    sees the full tree (needed to build a manifest that covers
    excluded content). Nested archives are NOT opened in this mode,
    so the nested-archive bomb vector stays unreachable. The
    structural unpack-guards (traversal, bomb, symlink,
    declared-size) are unchanged and still raise. The default
    ``classify=False`` preserves the byte-identical strict path used
    by production Stage 1 callers.
    """
    budget = _Budget(max_unzipped_size)
    yield from _extract_recursive(
        archive_bytes,
        archive_kind=archive_kind,
        budget=budget,
        max_unzipped_size=max_unzipped_size,
        max_nesting_depth=max_nesting_depth,
        allowed_extensions=allowed_extensions,
        current_depth=0,
        classify=classify,
        skip_matcher=skip_matcher,
    )


# ── Internal recursion ─────────────────────────────────────────────


def _extract_recursive(
    archive_bytes: bytes,
    *,
    archive_kind: str,
    budget: _Budget,
    max_unzipped_size: int,
    max_nesting_depth: int,
    allowed_extensions: frozenset[str],
    current_depth: int,
    classify: bool,
    skip_matcher: SkipMatcher | None,
) -> Iterator[ExtractedFile | ClassifiedEntry]:
    if current_depth >= max_nesting_depth:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"archive nesting depth {current_depth} reaches limit "
            f"{max_nesting_depth}; further recursion rejected",
        )

    if archive_kind == "zip":
        yield from _extract_zip(
            archive_bytes,
            budget=budget,
            max_unzipped_size=max_unzipped_size,
            max_nesting_depth=max_nesting_depth,
            allowed_extensions=allowed_extensions,
            current_depth=current_depth,
            classify=classify,
            skip_matcher=skip_matcher,
        )
    elif archive_kind == "tar.gz":
        yield from _extract_tar_gz(
            archive_bytes,
            budget=budget,
            max_unzipped_size=max_unzipped_size,
            max_nesting_depth=max_nesting_depth,
            allowed_extensions=allowed_extensions,
            current_depth=current_depth,
            classify=classify,
            skip_matcher=skip_matcher,
        )
    else:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"unsupported archive_kind {archive_kind!r}",
        )


def _extract_zip(
    archive_bytes: bytes,
    *,
    budget: _Budget,
    max_unzipped_size: int,
    max_nesting_depth: int,
    allowed_extensions: frozenset[str],
    current_depth: int,
    classify: bool,
    skip_matcher: SkipMatcher | None,
) -> Iterator[ExtractedFile | ClassifiedEntry]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"malformed zip archive: {exc}",
        ) from exc

    with zf:
        infos = zf.infolist()
        per_entry_cap = max(max_unzipped_size // 2, 1)

        # Pre-extract: total declared uncompressed size. Skip-zone
        # entries are exempt from accounting (№14), matched on the same
        # NFKC-normalized name the in-loop gate uses.
        declared_total = sum(
            info.file_size
            for info in infos
            if skip_matcher is None
            or skip_matcher(normalize_filename(info.filename)) is None
        )
        if declared_total > max_unzipped_size:
            raise SecurityRejectedError(
                ErrorCategory.ARCHIVE_VIOLATION,
                f"zip declared total size {declared_total} > "
                f"budget {max_unzipped_size}",
            )

        for info in infos:
            # Per-member jurisdiction order (№14): hostility -> skip ->
            # accounting -> extraction. Hostile shapes reject the whole
            # archive even under a skip prefix.
            arcname = _validate_arcname(info.filename)
            skip_prefix = skip_matcher(arcname) if skip_matcher is not None else None

            if info.is_dir():
                if skip_prefix is None:
                    _check_directory_depth(info.filename)
                continue  # hostility validated above; no content to yield

            # Symlink / device / fifo defense via Unix mode bits --
            # hostility, so it runs before the skip gate.
            mode = (info.external_attr >> 16) & _ZIP_MODE_MASK
            if mode and mode not in {_ZIP_MODE_REGULAR, _ZIP_MODE_DIRECTORY}:
                raise SecurityRejectedError(
                    ErrorCategory.ARCHIVE_VIOLATION,
                    f"non-regular zip entry {arcname!r} (Unix mode {mode:#o})",
                )

            if skip_prefix is not None:
                if classify:
                    yield ClassifiedEntry(
                        arcname=arcname,
                        content=b"",
                        depth=current_depth,
                        verdict=EntryVerdict.DENYLIST_SKIP,
                        declared_size=info.file_size,
                    )
                continue

            _check_directory_depth(info.filename)

            # Per-entry size cap (asymmetric bomb defense).
            if info.file_size > per_entry_cap:
                raise SecurityRejectedError(
                    ErrorCategory.ARCHIVE_VIOLATION,
                    f"zip entry {arcname!r} size {info.file_size} > "
                    f"per-entry cap {per_entry_cap}",
                )

            # Per-entry compression ratio.
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > _COMPRESSION_RATIO_LIMIT:
                    raise SecurityRejectedError(
                        ErrorCategory.ARCHIVE_VIOLATION,
                        f"zip entry {arcname!r} compression ratio "
                        f"{ratio:.0f}x > limit {_COMPRESSION_RATIO_LIMIT}",
                    )

            with zf.open(info, "r") as src:
                content = _read_chunked(src, budget)

            # Declared-vs-actual size mismatch.
            if info.file_size and len(content) != info.file_size:
                raise SecurityRejectedError(
                    ErrorCategory.ARCHIVE_VIOLATION,
                    f"zip entry {arcname!r} actual size {len(content)} != "
                    f"declared {info.file_size}",
                )

            yield from _yield_or_recurse(
                arcname=arcname,
                content=content,
                depth=current_depth,
                budget=budget,
                max_unzipped_size=max_unzipped_size,
                max_nesting_depth=max_nesting_depth,
                allowed_extensions=allowed_extensions,
                classify=classify,
                skip_matcher=skip_matcher,
            )


def _extract_tar_gz(
    archive_bytes: bytes,
    *,
    budget: _Budget,
    max_unzipped_size: int,
    max_nesting_depth: int,
    allowed_extensions: frozenset[str],
    current_depth: int,
    classify: bool,
    skip_matcher: SkipMatcher | None,
) -> Iterator[ExtractedFile | ClassifiedEntry]:
    try:
        tf = tarfile.open(  # noqa: SIM115 — context manager handled below
            fileobj=io.BytesIO(archive_bytes),
            mode="r:gz",
        )
    except tarfile.TarError as exc:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"malformed tar.gz archive: {exc}",
        ) from exc

    with tf:
        members = tf.getmembers()
        per_entry_cap = max(max_unzipped_size // 2, 1)

        # Pre-extract: total declared size (skip-zone entries exempt,
        # №14 -- same normalization as the in-loop gate).
        declared_total = sum(
            m.size
            for m in members
            if skip_matcher is None or skip_matcher(normalize_filename(m.name)) is None
        )
        if declared_total > max_unzipped_size:
            raise SecurityRejectedError(
                ErrorCategory.ARCHIVE_VIOLATION,
                f"tar.gz declared total size {declared_total} > "
                f"budget {max_unzipped_size}",
            )

        # Total compression ratio (gzip wraps whole tar).
        if archive_bytes:
            total_ratio = declared_total / len(archive_bytes)
            if total_ratio > _COMPRESSION_RATIO_LIMIT:
                raise SecurityRejectedError(
                    ErrorCategory.ARCHIVE_VIOLATION,
                    f"tar.gz total compression ratio {total_ratio:.0f}x > "
                    f"limit {_COMPRESSION_RATIO_LIMIT}",
                )

        for member in members:
            # Per-member jurisdiction order (№14): hostility -> skip ->
            # accounting -> extraction (mirror of the zip loop).
            arcname = _validate_arcname(member.name)
            skip_prefix = skip_matcher(arcname) if skip_matcher is not None else None

            if member.isdir():
                if skip_prefix is None:
                    _check_directory_depth(member.name)
                continue

            # Reject anything other than regular files. Symlinks,
            # hard links, devices, FIFOs, sparse files all rejected --
            # hostility, so it runs before the skip gate.
            if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                raise SecurityRejectedError(
                    ErrorCategory.ARCHIVE_VIOLATION,
                    f"non-regular tar entry {arcname!r} type {member.type!r}",
                )

            if skip_prefix is not None:
                if classify:
                    yield ClassifiedEntry(
                        arcname=arcname,
                        content=b"",
                        depth=current_depth,
                        verdict=EntryVerdict.DENYLIST_SKIP,
                        declared_size=member.size,
                    )
                continue

            _check_directory_depth(member.name)

            if member.size > per_entry_cap:
                raise SecurityRejectedError(
                    ErrorCategory.ARCHIVE_VIOLATION,
                    f"tar entry {arcname!r} size {member.size} > "
                    f"per-entry cap {per_entry_cap}",
                )

            extractor = tf.extractfile(member)
            if extractor is None:
                # Defensive: should be unreachable given the type check
                # above. tarfile returns None for non-regular members.
                continue

            with extractor as src:
                content = _read_chunked(src, budget)

            yield from _yield_or_recurse(
                arcname=arcname,
                content=content,
                depth=current_depth,
                budget=budget,
                max_unzipped_size=max_unzipped_size,
                max_nesting_depth=max_nesting_depth,
                allowed_extensions=allowed_extensions,
                classify=classify,
                skip_matcher=skip_matcher,
            )


def _yield_or_recurse(
    *,
    arcname: str,
    content: bytes,
    depth: int,
    budget: _Budget,
    max_unzipped_size: int,
    max_nesting_depth: int,
    allowed_extensions: frozenset[str],
    classify: bool,
    skip_matcher: SkipMatcher | None,
) -> Iterator[ExtractedFile | ClassifiedEntry]:
    """Yield ``content`` as an entry, or recurse if it's an archive.

    In strict mode (``classify=False``) this is the fail-closed
    content gate: nested archives recurse, non-whitelisted extensions
    and magic mismatches raise. In classify mode the same three
    content-level signals become annotated :class:`ClassifiedEntry`
    yields instead -- the caller sees the full tree. The upstream
    structural guards (in ``_extract_zip`` / ``_extract_tar_gz``) have
    already run and raise in both modes.
    """
    nested_kind = _archive_kind_for_arcname(arcname)
    if nested_kind is not None:
        if classify:
            # Never open a nested archive in classify mode: the
            # zip-bomb-via-recursion vector stays unreachable and the
            # caller records the entry as an opaque raw-hashed blob.
            yield ClassifiedEntry(
                arcname=arcname,
                content=content,
                depth=depth,
                verdict=EntryVerdict.NESTED_ARCHIVE,
                declared_size=len(content),
            )
            return
        yield from _extract_recursive(
            content,
            archive_kind=nested_kind,
            budget=budget,
            max_unzipped_size=max_unzipped_size,
            max_nesting_depth=max_nesting_depth,
            allowed_extensions=allowed_extensions,
            current_depth=depth + 1,
            classify=classify,
            skip_matcher=skip_matcher,
        )
        return

    ext = extension_of(arcname)
    if ext not in allowed_extensions:
        if classify:
            yield ClassifiedEntry(
                arcname=arcname,
                content=content,
                depth=depth,
                verdict=EntryVerdict.FORBIDDEN_TYPE,
                declared_size=len(content),
            )
            return
        raise SecurityRejectedError(
            ErrorCategory.FORBIDDEN_TYPE,
            f"archive entry {arcname!r} extension {ext!r} not in whitelist",
        )

    if classify:
        # Extension is whitelisted; the remaining content signal is the
        # magic gate, whose outcome maps to a verdict:
        #   * empty content -- no magic to check, not a mismatch; the
        #     extension is whitelisted, so INCLUDE (e.g. __init__.py).
        #   * MAGIC_MISMATCH -- genuine ext/content disagreement on a
        #     recognised extension (a .py carrying an ELF, a .pdf carrying
        #     a PE); a security signal -> surface as MAGIC_MISMATCH.
        #   * FORBIDDEN_TYPE -- the extension is whitelisted by the caller
        #     but absent from the magic layer's family table (.sql /
        #     .yaml / .go / ...). The magic layer has NO opinion; this is
        #     not a content mismatch, so INCLUDE it (the caller's
        #     allowlist already accepted the extension).
        if not content:
            yield ClassifiedEntry(
                arcname=arcname,
                content=content,
                depth=depth,
                verdict=EntryVerdict.INCLUDED,
                declared_size=len(content),
            )
            return
        try:
            verify_extension_matches_content(arcname, content)
        except SecurityRejectedError as exc:
            verdict = (
                EntryVerdict.MAGIC_MISMATCH
                if exc.category is ErrorCategory.MAGIC_MISMATCH
                else EntryVerdict.INCLUDED
            )
            yield ClassifiedEntry(
                arcname=arcname,
                content=content,
                depth=depth,
                verdict=verdict,
                declared_size=len(content),
            )
            return
        yield ClassifiedEntry(
            arcname=arcname,
            content=content,
            depth=depth,
            verdict=EntryVerdict.INCLUDED,
            declared_size=len(content),
        )
        return

    verify_extension_matches_content(arcname, content)

    yield ExtractedFile(arcname=arcname, content=content, depth=depth)


def _archive_kind_for_arcname(arcname: str) -> str | None:
    """Return ``"zip"`` / ``"tar.gz"`` if the arcname is recursable, else None.

    Compound suffix ``.tar.gz`` and the conventional ``.tgz`` alias
    map to the same archive_kind. Bare ``.gz`` (single-file gzip,
    not a tar) is intentionally NOT recursed -- it has no internal
    file structure to validate and is currently rejected upstream
    by extension whitelist if a caller doesn't allow it.
    """
    norm = normalize_filename(arcname).lower()
    if norm.endswith(".tar.gz") or norm.endswith(".tgz"):
        return "tar.gz"
    if norm.endswith(".zip"):
        return "zip"
    return None


def _validate_arcname(arcname: str) -> str:
    """Validate arcname against hostile shapes; return NFKC-normalized form.

    Hostility jurisdiction only (№14 split): empty, null byte,
    backslash, leading ``/``, ``..`` segment -- cheap-fail-fast order.
    Directory depth is resource accounting, not hostility; it lives in
    :func:`_check_directory_depth` and runs only for entries that
    survive the skip gate. Structural checks operate on the
    pre-normalized string so attempts to encode ``..`` as
    compatibility characters do not bypass the path check; the
    NFKC-normalized arcname is returned for downstream callers.
    """
    if not arcname:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            "empty arcname",
        )
    if "\x00" in arcname:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"null byte in arcname {arcname!r}",
        )
    if "\\" in arcname:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"backslash in arcname {arcname!r}",
        )
    if arcname.startswith("/"):
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"absolute arcname {arcname!r}",
        )

    parts = arcname.split("/")
    if any(p == ".." for p in parts):
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"path traversal in arcname {arcname!r}",
        )

    return normalize_filename(arcname)


def _check_directory_depth(arcname: str) -> None:
    """Reject arcnames nested deeper than ``_MAX_DIRECTORY_DEPTH``.

    Resource accounting, not a hostility gate (№14): callers run this
    only for entries that survive the skip gate. Operates on the
    pre-normalized string for the same bypass-resistance reasoning as
    :func:`_validate_arcname`.
    """
    cleaned = arcname.rstrip("/")
    depth = cleaned.count("/")
    if depth > _MAX_DIRECTORY_DEPTH:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"arcname {arcname!r} directory depth {depth} > {_MAX_DIRECTORY_DEPTH}",
        )


def _read_chunked(stream: IO[bytes], budget: _Budget) -> bytes:
    """Read all bytes from ``stream`` in chunks, charging the budget.

    Raises :class:`SecurityRejectedError` (``ARCHIVE_VIOLATION``)
    via the budget counter as soon as cumulative reads exceed the
    limit; callers do not need to wrap the return value.
    """
    buf = bytearray()
    while True:
        chunk = stream.read(_CHUNK_SIZE)
        if not chunk:
            break
        budget.consume(len(chunk))
        buf.extend(chunk)
    return bytes(buf)


# ── Submission extraction (Phase 2.1 C2 migration per KD-2.1-H) ───
#
# extract_submission_content + helpers migrated from safety/archive.py.
# Adapted to raise canonical SecurityRejectedError with ErrorCategory
# values (ARCHIVE_BOMB / SYMLINK_VIOLATION) instead of legacy
# SymlinkViolationError / ArchiveBombError subclasses. Per KD-2.1-I
# 2-set ratify: PATH_TRAVERSAL + CONTENT_SAFETY values NOT promoted
# (legacy classes had 0 raisers in production — verified at Phase 2.1
# pre-flight 2026-05-11).
#
# Helpers prefixed _submission_* to coexist with extract_archive_safely
# helpers above. Settings prefix kept as safety_archive_* for backward-
# compat with deployed configs; not renamed in C2.

# Text-like extensions for content extraction (kept as legacy set —
# no functional change vs safety/archive.py:24-63).
_SUBMISSION_TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".java",
        ".c",
        ".cpp",
        ".cs",
        ".sql",
        ".md",
        ".txt",
        ".html",
        ".htm",
        ".ipynb",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".cfg",
        ".ini",
        ".xml",
        ".csv",
        ".sh",
        ".bash",
        ".css",
        ".jsx",
        ".tsx",
        ".rb",
        ".go",
        ".rs",
        ".kt",
        ".swift",
        ".r",
        ".R",
        ".scala",
        ".h",
        ".hpp",
    }
)


def _submission_decode_bytes(raw: bytes) -> str:
    """Decode raw bytes to text, trying common encodings."""
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace")


def _submission_read_text_file(path: Path) -> str:
    """Read a file as text, trying common encodings."""
    return _submission_decode_bytes(path.read_bytes())


def _submission_is_text_file(filename: str) -> bool:
    """Check if a filename has a text-like extension."""
    return Path(filename).suffix.lower() in _SUBMISSION_TEXT_EXTENSIONS


def _submission_is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    """Check if a ZIP entry is a symbolic link (Unix external attributes)."""
    return stat.S_ISLNK(info.external_attr >> 16)


def _submission_sanitize_filename(raw_name: str) -> str | None:
    """Sanitize an archive entry filename, stripping path traversal.

    Returns ``None`` if the name is unsafe and should be skipped
    entirely (e.g. resolves to empty after stripping or is an
    absolute path that fully resolves out of bounds).
    """
    parts = PurePosixPath(raw_name).parts
    clean_parts = [p for p in parts if p not in ("/", "..")]
    if not clean_parts:
        return None
    return str(PurePosixPath(*clean_parts))


async def extract_submission_content(file_path: Path) -> SubmissionContent:
    """Extract text content from a submission file or archive.

    Migrated from ``safety/archive.py:109`` per Phase 2.1 C2
    (KD-2.1-H). Adapted to raise canonical
    :class:`SecurityRejectedError` with :class:`ErrorCategory`
    values instead of legacy subclass hierarchy:

    * Symlink rejection: ``ErrorCategory.SYMLINK_VIOLATION``
      (was ``SymlinkViolationError``).
    * Bomb / size / count rejection: ``ErrorCategory.ARCHIVE_BOMB``
      (was ``ArchiveBombError``).

    Supports:

    * Single text files (read directly).
    * ``.zip`` archives (extract with bomb protection).
    * ``.gz`` files (decompress single file).

    Limits configurable via environment variables (``safety_archive_*``
    settings — prefix kept for backward-compat with deployed configs).

    Args:
        file_path: Path to the submission file.

    Returns:
        :class:`SubmissionContent` with extracted file contents and
        any non-fatal :class:`SecurityWarning` observations.

    Raises:
        SecurityRejectedError: If the file is a symlink
            (``SYMLINK_VIOLATION``), archive exceeds decompression
            bomb limits (``ARCHIVE_BOMB``), or archive structure is
            malformed (``ARCHIVE_VIOLATION`` — captures
            ``BadZipFile`` / ``BadGzipFile`` per canonical exception
            consolidation, mirroring :func:`extract_archive_safely`
            precedent in the same module).
    """
    if await anyio.Path(file_path).is_symlink():
        raise SecurityRejectedError(
            ErrorCategory.SYMLINK_VIOLATION,
            f"refusing to process symlink: {file_path.name}",
        )

    suffix = file_path.suffix.lower()

    if suffix == ".zip":
        return await anyio.to_thread.run_sync(_extract_submission_zip, file_path)
    if suffix == ".gz":
        return await anyio.to_thread.run_sync(_extract_submission_gz, file_path)
    return await anyio.to_thread.run_sync(_read_single_submission_file, file_path)


def _read_single_submission_file(file_path: Path) -> SubmissionContent:
    """Read a single text file into a :class:`SubmissionContent`."""
    content = _submission_read_text_file(file_path)
    fc = FileContent(
        filename=file_path.name,
        content=content,
        size=len(content.encode("utf-8")),
    )
    return SubmissionContent(files=[fc], total_size=fc.size)


def _extract_submission_zip(file_path: Path) -> SubmissionContent:
    """Extract a ZIP archive with decompression bomb protection."""
    settings = get_settings()
    max_bytes = settings.safety_archive_max_uncompressed_mb * 1024 * 1024
    max_files = settings.safety_archive_max_files
    max_nesting = settings.safety_archive_max_nesting

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            warnings: list[SecurityWarning] = []

            # Check for nested archives
            for info in zf.infolist():
                nested_suffix = PurePosixPath(info.filename).suffix.lower()
                if (
                    nested_suffix in (".zip", ".gz", ".tar", ".bz2", ".xz")
                    and max_nesting < 2
                ):
                    raise SecurityRejectedError(
                        ErrorCategory.ARCHIVE_BOMB,
                        (
                            f"nested archive detected: {info.filename}; "
                            f"max nesting depth: {max_nesting}"
                        ),
                    )

            # Filter symlinks and collect warnings
            all_entries = [i for i in zf.infolist() if not i.is_dir()]
            infos: list[zipfile.ZipInfo] = []
            for entry in all_entries:
                if _submission_is_zip_symlink(entry):
                    warnings.append(
                        SecurityWarning(
                            violation_type="symlink",
                            message=f"Symlink entry skipped: {entry.filename}",
                            raw_filename=entry.filename,
                        )
                    )
                    continue
                infos.append(entry)

            if len(infos) > max_files:
                raise SecurityRejectedError(
                    ErrorCategory.ARCHIVE_BOMB,
                    (f"archive contains {len(infos)} files; max allowed: {max_files}"),
                )

            # Check total uncompressed size
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > max_bytes:
                raise SecurityRejectedError(
                    ErrorCategory.ARCHIVE_BOMB,
                    (
                        f"archive uncompressed size {total_uncompressed} "
                        f"bytes exceeds limit {max_bytes} bytes"
                    ),
                )

            files: list[FileContent] = []
            total_size = 0

            for info in infos:
                safe_name = _submission_sanitize_filename(info.filename)
                if safe_name is None:
                    warnings.append(
                        SecurityWarning(
                            violation_type="path_traversal",
                            message=(f"Entry resolves to empty path: {info.filename}"),
                            raw_filename=info.filename,
                        )
                    )
                    continue

                if safe_name != info.filename:
                    warnings.append(
                        SecurityWarning(
                            violation_type="path_traversal",
                            message=(
                                f"Path traversal sanitized: "
                                f"{info.filename} -> {safe_name}"
                            ),
                            raw_filename=info.filename,
                            filename=safe_name,
                        )
                    )

                if not _submission_is_text_file(safe_name):
                    logger.debug(
                        "skipping_non_text_file",
                        filename=safe_name,
                    )
                    continue

                raw = zf.read(info.filename)
                total_size += len(raw)

                if total_size > max_bytes:
                    raise SecurityRejectedError(
                        ErrorCategory.ARCHIVE_BOMB,
                        (
                            f"cumulative extracted size {total_size} bytes "
                            f"exceeds limit {max_bytes} bytes"
                        ),
                    )

                text = _submission_decode_bytes(raw)

                files.append(
                    FileContent(
                        filename=safe_name,
                        content=text,
                        size=len(raw),
                    )
                )

            return SubmissionContent(
                files=files,
                total_size=total_size,
                security_warnings=warnings,
            )

    except zipfile.BadZipFile as exc:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"invalid ZIP file: {exc}",
        ) from exc


def _extract_submission_gz(file_path: Path) -> SubmissionContent:
    """Decompress a .gz file (single file compression)."""
    settings = get_settings()
    max_bytes = settings.safety_archive_max_uncompressed_mb * 1024 * 1024

    try:
        with gzip.open(file_path, "rb") as f:
            raw = f.read(max_bytes + 1)

        if len(raw) > max_bytes:
            raise SecurityRejectedError(
                ErrorCategory.ARCHIVE_BOMB,
                f"decompressed size exceeds limit {max_bytes} bytes",
            )

        # Determine inner filename (strip .gz)
        inner_name = file_path.stem
        text = _submission_decode_bytes(raw)

        fc = FileContent(filename=inner_name, content=text, size=len(raw))
        return SubmissionContent(files=[fc], total_size=fc.size)

    except gzip.BadGzipFile as exc:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"invalid gzip file: {exc}",
        ) from exc
