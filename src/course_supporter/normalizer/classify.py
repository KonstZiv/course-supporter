"""Deterministic classification for the normalizer (KD18 P1).

Two pure, archive-I/O-free concerns:

* :class:`ExtensionClassifier` -- maps a path's extension to an
  :class:`EntryClass` (text / document / binary). It rejects nothing;
  an unknown extension is ``BINARY`` (hash-tracked), never dropped.
* Denylist matching -- :func:`denylist_prefix` decides, per path
  component (never substring / prefix), whether a path sits under a
  denied directory; :func:`collapse_denylist` folds all denied entries
  under the same highest-occurrence prefix into one ExcludedEntry row.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Final

from course_supporter.normalizer.models import (
    EntryClass,
    ExcludedEntry,
    ExcludedReason,
)

# ── Extension → class map ──────────────────────────────────────────

_TEXT_EXTS: Final[frozenset[str]] = frozenset(
    {
        "py",
        "js",
        "ts",
        "java",
        "c",
        "cpp",
        "cs",
        "go",
        "rs",
        "rb",
        "php",
        "sql",
        "sh",
        "md",
        "txt",
        "rst",
        "html",
        "css",
        "scss",
        "json",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "xml",
        "csv",
        "ipynb",
    }
)
_DOCUMENT_EXTS: Final[frozenset[str]] = frozenset({"docx", "pdf"})

# The normalizer's own allowlist, passed to ``extract_archive_safely``
# in classify mode. Everything outside it surfaces as FORBIDDEN_TYPE and
# is kept as a BINARY manifest entry -- never rejected.
KNOWN_EXTENSIONS: Final[frozenset[str]] = _TEXT_EXTS | _DOCUMENT_EXTS


def _extension_of(path: str) -> str:
    """Lower-cased extension of the LAST path component, without the dot.

    Uses ``PurePosixPath.suffix`` so a dot in a parent directory
    (``my.pkg/Makefile``) never leaks into the extension; dotfiles
    (``.gitignore``) have no suffix and read as ``""``.
    """
    return PurePosixPath(path).suffix.lower().removeprefix(".")


class ExtensionClassifier:
    """Extension-based ``EntryClassifier`` strategy (port implementation)."""

    def classify(self, path: str, head: bytes) -> EntryClass:
        ext = _extension_of(path)
        if ext in _TEXT_EXTS:
            return EntryClass.TEXT
        if ext in _DOCUMENT_EXTS:
            return EntryClass.DOCUMENT
        return EntryClass.BINARY


# ── Denylist (component-based) ─────────────────────────────────────

_DENYLIST_EXACT: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".idea",
        ".vscode",
        "dist",
        "build",
        ".DS_Store",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "htmlcov",
        ".ipynb_checkpoints",
        "target",
        ".gradle",
        "bin",
        "obj",
        # №14 expansion (operator-ratified 2026-07-12; prod blast-radius
        # on stored KD18 manifests verified empty before changing the
        # canonical list): OS junk that Finder/Explorer packs into
        # archives and framework caches that push honest lesson
        # archives over structural limits.
        "__MACOSX",
        ".Trashes",
        ".angular",
        ".next",
        ".nuxt",
        ".cxx",
        ".externalNativeBuild",
        "captures",
    }
)
_DENYLIST_GLOBS: Final[tuple[str, ...]] = (
    "*.egg-info",
    # №14 expansion — file-name patterns, matched per segment (so an
    # AppleDouble companion matches both as a leaf and as a directory).
    "._*",  # AppleDouble companions (macOS / iPadOS Finder family)
    "~$*",  # MS Office lock files
    ".Trash-*",  # per-user trash dirs (Linux desktop convention)
    "Thumbs.db",
    "Desktop.ini",
    "local.properties",
)


def _is_denied_segment(segment: str) -> bool:
    # fnmatchcase (not fnmatch) so matching is platform-deterministic;
    # the glob is applied to the SEGMENT, never the whole path.
    return segment in _DENYLIST_EXACT or any(
        fnmatchcase(segment, glob) for glob in _DENYLIST_GLOBS
    )


def denylist_prefix(path: str) -> str | None:
    """Return the collapse prefix if ``path`` sits under a denied segment.

    Matching is per path component (``PurePosixPath.parts``) by equality
    / per-segment glob -- never substring or prefix -- so ``.venv``
    matches ``a/.venv/x`` but not ``.venvrc`` or ``my.venv.backup/``, and
    ``bin`` matches ``bin/`` but not ``mybin/`` or ``bin.txt``.

    The prefix is the path up to and including the FIRST (highest)
    denied segment; a trailing ``/`` marks a directory collapse (more
    segments follow), its absence a denied leaf file (e.g. ``.DS_Store``).
    Returns ``None`` when no component is denied.
    """
    parts = PurePosixPath(path).parts
    for i, segment in enumerate(parts):
        if _is_denied_segment(segment):
            prefix = "/".join(parts[: i + 1])
            if i + 1 < len(parts):
                prefix += "/"
            return prefix
    return None


def collapse_denylist(
    entries: Sequence[tuple[str, int]],
) -> tuple[ExcludedEntry, ...]:
    """Fold denied ``(path, size)`` entries into one row per prefix.

    Entries whose path is not under any denied segment are ignored (the
    caller keeps them as survivors). Denied entries sharing a
    highest-occurrence prefix collapse into a single
    :class:`ExcludedEntry` (``entries`` = count, ``size`` = sum). A
    nested denylist directory does not spawn a second row -- its files
    already fall under the outer prefix (``denylist_prefix`` returns the
    first match). Rows are returned sorted by prefix for determinism.
    """
    groups: dict[str, list[int]] = {}
    for path, size in entries:
        prefix = denylist_prefix(path)
        if prefix is None:
            continue
        groups.setdefault(prefix, []).append(size)
    return tuple(
        ExcludedEntry(
            path=prefix,
            reason=ExcludedReason.DENYLIST_DIR,
            entries=len(sizes),
            size=sum(sizes),
        )
        for prefix, sizes in sorted(groups.items())
    )
