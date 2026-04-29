"""Safe archive extraction for KD14 Stage 1 (KD-A).

Wraps stdlib ``zipfile`` / ``tarfile`` with multi-layer attack defense.
Reads in chunks against a shared byte budget; never delegates to
``extractall`` (which would happily honor path traversal); validates
each entry syntactically and structurally before yielding.

## Defense layers

1. **Path traversal** -- arcname syntactic validation: empty, null
   byte, backslash, leading ``/``, ``..`` segments, directory depth
   ``> _MAX_DIRECTORY_DEPTH`` (8) all raise ``ARCHIVE_VIOLATION``.
   ``zipfile.extractall`` / ``tarfile.extractall`` are explicitly
   not used; iteration is manual per entry.

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
     > _MAX_DIRECTORY_DEPTH`` (8) rejects (DoS guard, vision §KD14
     ambiguous).
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
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Literal

from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)
from course_supporter.security.file_type import (
    extension_of,
    verify_extension_matches_content,
)
from course_supporter.security.normalization import normalize_filename

# Hardcoded directory-depth limit (DoS guard). Vision §KD14 says
# "max depth 3" without specifying directory vs archive recursion;
# we apply directory depth as a separate, generous cap.
_MAX_DIRECTORY_DEPTH = 8

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


def extract_archive_safely(
    archive_bytes: bytes,
    *,
    archive_kind: Literal["zip", "tar.gz"],
    max_unzipped_size: int,
    max_nesting_depth: int,
    allowed_extensions: frozenset[str],
) -> Iterator[ExtractedFile]:
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
            for a non-archive entry.
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
) -> Iterator[ExtractedFile]:
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
        )
    elif archive_kind == "tar.gz":
        yield from _extract_tar_gz(
            archive_bytes,
            budget=budget,
            max_unzipped_size=max_unzipped_size,
            max_nesting_depth=max_nesting_depth,
            allowed_extensions=allowed_extensions,
            current_depth=current_depth,
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
) -> Iterator[ExtractedFile]:
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

        # Pre-extract: total declared uncompressed size.
        declared_total = sum(info.file_size for info in infos)
        if declared_total > max_unzipped_size:
            raise SecurityRejectedError(
                ErrorCategory.ARCHIVE_VIOLATION,
                f"zip declared total size {declared_total} > "
                f"budget {max_unzipped_size}",
            )

        for info in infos:
            arcname = _validate_arcname(info.filename)

            if info.is_dir():
                continue  # path validated above; no content to yield

            # Symlink / device / fifo defense via Unix mode bits.
            mode = (info.external_attr >> 16) & _ZIP_MODE_MASK
            if mode and mode not in {_ZIP_MODE_REGULAR, _ZIP_MODE_DIRECTORY}:
                raise SecurityRejectedError(
                    ErrorCategory.ARCHIVE_VIOLATION,
                    f"non-regular zip entry {arcname!r} (Unix mode {mode:#o})",
                )

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
            )


def _extract_tar_gz(
    archive_bytes: bytes,
    *,
    budget: _Budget,
    max_unzipped_size: int,
    max_nesting_depth: int,
    allowed_extensions: frozenset[str],
    current_depth: int,
) -> Iterator[ExtractedFile]:
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

        # Pre-extract: total declared size.
        declared_total = sum(m.size for m in members)
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
            arcname = _validate_arcname(member.name)

            if member.isdir():
                continue

            # Reject anything other than regular files. Symlinks,
            # hard links, devices, FIFOs, sparse files all rejected.
            if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                raise SecurityRejectedError(
                    ErrorCategory.ARCHIVE_VIOLATION,
                    f"non-regular tar entry {arcname!r} type {member.type!r}",
                )

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
) -> Iterator[ExtractedFile]:
    """Yield ``content`` as ExtractedFile, or recurse if it's an archive."""
    nested_kind = _archive_kind_for_arcname(arcname)
    if nested_kind is not None:
        yield from _extract_recursive(
            content,
            archive_kind=nested_kind,
            budget=budget,
            max_unzipped_size=max_unzipped_size,
            max_nesting_depth=max_nesting_depth,
            allowed_extensions=allowed_extensions,
            current_depth=depth + 1,
        )
        return

    ext = extension_of(arcname)
    if ext not in allowed_extensions:
        raise SecurityRejectedError(
            ErrorCategory.FORBIDDEN_TYPE,
            f"archive entry {arcname!r} extension {ext!r} not in whitelist",
        )

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
    """Validate arcname syntactically; return NFKC-normalized form.

    Defenses applied in this order (cheap-fail-fast):
    empty, null byte, backslash, leading ``/``, ``..`` segment,
    directory depth. Returns the NFKC-normalized arcname for
    downstream callers; depth count and structural checks operate
    on the pre-normalized string so attempts to encode ``..`` as
    compatibility characters do not bypass the path check.
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

    cleaned = arcname.rstrip("/")
    depth = cleaned.count("/")
    if depth > _MAX_DIRECTORY_DEPTH:
        raise SecurityRejectedError(
            ErrorCategory.ARCHIVE_VIOLATION,
            f"arcname {arcname!r} directory depth {depth} > {_MAX_DIRECTORY_DEPTH}",
        )

    return normalize_filename(arcname)


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
