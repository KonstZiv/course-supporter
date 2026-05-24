"""Magic bytes file type detection (vision §KD14, KD-B).

Wraps libmagic via ``python-magic`` to derive a file's MIME type
from its actual content header rather than its filename extension.
The Stage 1 orchestrator uses :func:`verify_extension_matches_content`
to enforce KD14's "extension matches content" invariant before any
whitelist resolution -- a file named ``homework.pdf`` carrying a
PE binary header is rejected with category ``MAGIC_MISMATCH``;
whitelist policy lookup (commit f) operates on the content-derived
MIME type, never on the filename extension.

## Performance contract

``magic.from_buffer`` reads only the first ~2KB of input.
Stream-based callers in commit (g) should pass
``stream.read(4096)`` + ``stream.seek(0)`` rather than the full
content -- a 5GB video upload must not be loaded into memory just
to derive its MIME type.

The libmagic database is loaded once at module import via the
module-scope ``_MAGIC_*`` instances; subsequent calls share the
parsed cache and execute in microseconds per invocation.

## Single-responsibility boundary

This module returns raw MIME type strings and does not consult
any whitelist. Policy decisions ("is application/pdf allowed for
the homework context?") live in
:mod:`course_supporter.security.policies` (commit f). Strict SRP
keeps the libmagic dependency surface tight and makes the wrapper
easily mockable for orchestrator tests.

## Charset detection

:func:`detect_charset` is provided as a complementary helper
returning libmagic's encoding label. The Stage 1 orchestrator
(commit g) decides the policy: hard-reject non-UTF-8, warn, or
transparently convert. This module does not implement that policy.
"""

from __future__ import annotations

import magic

from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)
from course_supporter.security.normalization import normalize_filename

# libmagic database is loaded once per instance. Keeping the two
# concerns (mime / encoding) on separate instances avoids parsing
# the combined "type; charset=..." string and makes each call's
# intent obvious at the call site.
_MAGIC_MIME = magic.Magic(mime=True)
_MAGIC_ENCODING = magic.Magic(mime_encoding=True)


# Map known filename extensions to acceptable MIME-type family
# prefixes. The mismatch check accepts content whose detected MIME
# starts with any listed family for the given extension; that
# tolerance lets libmagic's detailed detection (``application/zip``
# for ``.pptx``, exact subtype for office formats) match the
# extension's broader expectation. Whitelist-based context policy
# (commit f) then decides which extensions are allowed where.
_EXTENSION_TO_MIME_FAMILIES: dict[str, tuple[str, ...]] = {
    "pdf": ("application/pdf",),
    "txt": ("text/",),
    "md": ("text/",),
    "markdown": ("text/",),
    "html": ("text/",),
    "htm": ("text/",),
    "csv": ("text/", "application/csv"),
    "json": ("application/json", "text/"),
    "xml": ("application/xml", "text/xml", "text/"),
    "doc": ("application/msword", "application/cdfv2"),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    ),
    "ppt": ("application/vnd.ms-powerpoint", "application/cdfv2"),
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip",
    ),
    "xls": ("application/vnd.ms-excel", "application/cdfv2"),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
    ),
    "zip": ("application/zip",),
    "gz": ("application/gzip",),
    "tgz": ("application/gzip",),
    "tar": ("application/x-tar",),
    "png": ("image/png",),
    "jpg": ("image/jpeg",),
    "jpeg": ("image/jpeg",),
    "gif": ("image/gif",),
    "webp": ("image/webp",),
    "mp3": ("audio/mpeg",),
    "wav": ("audio/wav", "audio/x-wav"),
    "m4a": ("audio/mp4", "audio/x-m4a"),
    "ogg": ("audio/ogg", "audio/x-ogg", "application/ogg"),
    "flac": ("audio/flac", "audio/x-flac"),
    "mp4": ("video/mp4",),
    "webm": ("video/webm",),
    "mov": ("video/quicktime", "video/mp4"),
    "avi": ("video/x-msvideo",),
    "mkv": ("video/x-matroska", "video/webm"),
    "ipynb": ("application/json", "text/"),
    "py": ("text/",),
    "js": ("application/javascript", "application/x-javascript", "text/"),
}


def detect_mime_type(content: bytes) -> str:
    """Return libmagic's MIME type for ``content``.

    Reads only the first ~2KB; callers with large payloads must
    slice the head themselves before invoking. Empty input returns
    ``"application/x-empty"`` per libmagic convention.

    Args:
        content: Raw bytes (head suffices). Empty input is allowed
            but returns the empty-marker MIME.

    Returns:
        MIME type string per libmagic, e.g. ``"application/pdf"``,
        ``"text/plain"``, ``"image/png"``,
        ``"application/x-empty"``.
    """
    return _MAGIC_MIME.from_buffer(content)


def detect_charset(content: bytes) -> str | None:
    """Return libmagic's encoding label, or ``None`` for binary content.

    The Stage 1 orchestrator (commit g) decides the policy on what
    to do with non-UTF-8 text content. This helper just surfaces
    the detected encoding.

    Args:
        content: Raw bytes (head suffices). Empty input returns
            ``None``.

    Returns:
        Encoding string (e.g. ``"utf-8"``, ``"windows-1251"``,
        ``"us-ascii"``), or ``None`` if libmagic classifies the
        content as binary or input is empty.
    """
    if not content:
        return None
    encoding = _MAGIC_ENCODING.from_buffer(content)
    if encoding == "binary":
        return None
    return encoding


def extension_of(filename: str) -> str:
    """Return lower-cased extension without leading dot, or empty.

    Returns the lowest-level suffix only (no compound recognition):
    ``"foo.tar.gz"`` returns ``"gz"``. If compound recognition is
    required (e.g. for archive-kind detection), implement a separate
    ``is_compound_archive`` helper -- do not extend this signature.

    Operates on the NFKC-normalized filename so compatibility-encoded
    extensions (e.g. full-width Latin "PDF" at U+FF30/U+FF24/U+FF26)
    collapse to ASCII before lookup. Filenames with no extension or
    whose entire content is a single token return the empty string.
    """
    normalized = normalize_filename(filename)
    head, sep, tail = normalized.rpartition(".")
    if not sep or not head:
        # No dot, or filename starts with the dot (e.g. ".bashrc")
        # -- treat as no recognised extension.
        return ""
    return tail.lower()


def verify_extension_matches_content(filename: str, content: bytes) -> None:
    """Raise :class:`SecurityRejectedError` if extension/content disagree.

    Empty content is rejected with ``MAGIC_MISMATCH`` -- a file
    claiming any extension but carrying zero bytes can not be
    validated against its declared type.

    Filenames without a recognised extension are rejected with
    ``FORBIDDEN_TYPE`` -- the whitelist resolution in commit (f)
    expects a recognised extension to map to allowed MIME types.

    Args:
        filename: User-supplied filename. NFKC normalization is
            applied internally for deterministic extension parsing;
            pass the original filename as-is.
        content: Bytes head sufficient for magic detection (~2KB).
            Callers with stream input should pass
            ``stream.read(4096)`` + ``stream.seek(0)`` per the
            module performance contract.

    Raises:
        SecurityRejectedError: with category ``MAGIC_MISMATCH`` on
            empty content or extension/content disagreement;
            ``FORBIDDEN_TYPE`` on missing or unrecognised extension.
    """
    if not content:
        raise SecurityRejectedError(
            ErrorCategory.MAGIC_MISMATCH,
            f"empty content for {filename!r}",
        )

    ext = extension_of(filename)
    if not ext:
        raise SecurityRejectedError(
            ErrorCategory.FORBIDDEN_TYPE,
            f"missing or unrecognised extension in {filename!r}",
        )

    expected_families = _EXTENSION_TO_MIME_FAMILIES.get(ext)
    if expected_families is None:
        raise SecurityRejectedError(
            ErrorCategory.FORBIDDEN_TYPE,
            f"unrecognised extension {ext!r} in {filename!r}",
        )

    detected = detect_mime_type(content)
    if not any(detected.startswith(family) for family in expected_families):
        raise SecurityRejectedError(
            ErrorCategory.MAGIC_MISMATCH,
            (
                f"extension {ext!r} expects {expected_families} "
                f"but content is {detected!r}"
            ),
        )
