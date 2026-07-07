"""Deterministic Mentor delta-context builder (KD18 P4).

Pure, zero-I/O assembly of the rich ``submission_text`` a project
submission hands the safety -> sanity -> review stages. Replaces P3's
bounded interim text at the ``tasks.py`` seam: the flat ``str`` contract
downstream is unchanged (G2 -- stage signatures untouched), but the
string now carries a structured delta context instead of a
whole-submission dump.

The builder does NO I/O: raw entry bytes are pulled through an injected
``read_text`` callable (the worker supplies a closure over the two
snapshot zips), so every branch -- priority, budget, H-c whole-vs-diff,
neighbour selection, delimiter escaping -- is unit-testable with a stub
dict and no S3 / zipfile.

Layout (one string, rendered INSIDE the template's untrusted
``<student_submission>`` / ``<submission>`` envelope, so the trusted
block self-declares "system-computed"):

* TRUSTED (system-computed facts): a base-manifest pseudo-tree + the
  two-level delta (content: changed/new/deleted; hygiene: new excluded
  vs base) + F2 metrics + a staleness line.
* UNTRUSTED file blocks, each self-describing (type + path): NEW whole,
  CHANGED-FULL (<= :data:`H_C_WHOLE_MAX_BYTES`) or CHANGED-DIFF (unified
  diff), DELETED as a path list only (in the trusted delta). Bodies
  escape the structural sentinel so student content cannot break out of
  its slot.

Budget H2: :data:`MENTOR_CONTEXT_MAX_BYTES` caps the assembly. Trusted is
always emitted; untrusted units are added by priority (changed -> new ->
neighbours) until the next would overflow, then dropped WHOLE (never
truncated) with a "skipped" manifest marker.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from typing import Final, Literal

from course_supporter.normalizer import Delta, Manifest, ManifestEntry

# ── Tuning constants (calibration candidates) ──────────────────────────────
# A CHANGED file at or below this raw (submission-side) size is rendered whole
# with a marker; a larger one is rendered as a unified diff (stdlib difflib).
# This is P4's OWN threshold -- deliberately NOT ``kept_single_max_bytes`` (a
# normalizer knob, unrelated; see normalizer/models.py).
H_C_WHOLE_MAX_BYTES: Final[int] = 64 * 1024

# Assembly budget for the whole context string, below the ~1 MB downstream
# reference cap (downstream does not re-truncate -- the flat str is consumed
# as-is by safety/sanity/review). Trusted is mandatory; untrusted bodies are
# added by priority until this is hit, then dropped whole with a marker.
MENTOR_CONTEXT_MAX_BYTES: Final[int] = 512 * 1024

Side = Literal["base", "sub"]
ReadText = Callable[[Side, ManifestEntry], str | None]

# Structural sentinel. EVERY delimiter line contains it, so escaping it in an
# untrusted body (exact-match) makes it impossible for student content to forge
# a boundary and break out of its slot. The escaped form does not itself
# contain the sentinel, so escaping is idempotent and non-recursive.
_SENTINEL: Final[str] = "@@MENTOR_CTX@@"
_SENTINEL_ESCAPED: Final[str] = "@@MENTOR_CTX_ESCAPED@@"
_BODY_UNAVAILABLE: Final[str] = "(body unavailable -- binary or non-extractable)"


def build_mentor_context(
    *,
    base_manifest: Manifest,
    sub_manifest: Manifest,
    delta: Delta,
    read_text: ReadText,
    base_version: int | None,
    latest_version: int | None,
) -> str:
    """Assemble the structured Mentor delta context as one flat string.

    Args:
        base_manifest: The base snapshot manifest (empty manifest when no
            base is attached -- the delta is then "all new").
        sub_manifest: The submission snapshot manifest.
        delta: ``compute_delta(base_manifest, sub_manifest)`` -- carries
            only paths; sizes/classes are resolved here from the two
            manifests' ``included`` entries.
        read_text: Injected byte->text reader. ``read_text(side, entry)``
            returns the extracted text of ``entry`` on ``side``
            (``"base"`` / ``"sub"``), or ``None`` for a binary /
            non-extractable entry. The builder never touches S3 or zip.
        base_version: The attached base's version, or ``None`` if no base.
        latest_version: The latest READY base version (for the staleness
            line), or ``None`` if unknown / no base.

    Returns:
        The assembled context: a trusted system-computed block followed by
        priority-ordered untrusted file blocks, bounded by
        :data:`MENTOR_CONTEXT_MAX_BYTES`, with a "skipped" marker for every
        file dropped whole on overflow.
    """
    sub_by = {entry.path: entry for entry in sub_manifest.included}
    base_by = {entry.path: entry for entry in base_manifest.included}

    trusted = _render_trusted(
        base_manifest, sub_manifest, delta, base_version, latest_version
    )
    used = len(trusted.encode("utf-8"))
    included: list[str] = []
    skipped: list[str] = []

    for kind, size_entry, block in _ordered_units(delta, sub_by, base_by, read_text):
        # +1 approximates the newline that joins this block into the output.
        block_bytes = len(block.encode("utf-8")) + 1
        if used + block_bytes <= MENTOR_CONTEXT_MAX_BYTES:
            included.append(block)
            used += block_bytes
        else:
            skipped.append(
                f"{_SENTINEL} SKIPPED path={_inline(size_entry.path)} change={kind} "
                f"size={_human_size(size_entry.size)} hash={size_entry.hash[:12]}"
            )

    parts = [f"{_SENTINEL} BEGIN", trusted, *included, *skipped, f"{_SENTINEL} END"]
    return "\n".join(parts)


# ── Ordering / unit construction (priority: changed -> new -> neighbours) ──


def _ordered_units(
    delta: Delta,
    sub_by: dict[str, ManifestEntry],
    base_by: dict[str, ManifestEntry],
    read_text: ReadText,
) -> list[tuple[str, ManifestEntry, str]]:
    """Priority-ordered untrusted blocks as ``(kind, size_entry, block)``.

    ``size_entry`` backs the overflow marker; ``block`` is the fully
    rendered, escaped file block. Deleted files carry no body (they are
    already listed in the trusted delta), so they produce no unit.
    """
    units: list[tuple[str, ManifestEntry, str]] = []

    for path in delta.changed:  # sorted by compute_delta
        sub_entry = sub_by.get(path)
        if sub_entry is None:
            continue
        block = _changed_block(path, sub_entry, base_by, read_text)
        units.append(("changed", sub_entry, block))

    for path in delta.new:  # sorted
        sub_entry = sub_by.get(path)
        if sub_entry is None:
            continue
        body = read_text("sub", sub_entry)
        units.append(
            ("new", sub_entry, _file_block("NEW", path, body, "new file, whole body"))
        )

    for path in _neighbour_paths(delta, sub_by, base_by, read_text):
        base_entry = base_by[path]
        body = read_text("base", base_entry)
        units.append(
            (
                "neighbour",
                base_entry,
                _file_block(
                    "NEIGHBOR",
                    path,
                    body,
                    "unchanged base file referenced by a changed file",
                ),
            )
        )

    return units


def _changed_block(
    path: str,
    sub_entry: ManifestEntry,
    base_by: dict[str, ManifestEntry],
    read_text: ReadText,
) -> str:
    """Render one CHANGED file: whole below the H-c threshold, else a diff."""
    if sub_entry.size <= H_C_WHOLE_MAX_BYTES:
        body = read_text("sub", sub_entry)
        return _file_block("CHANGED-FULL", path, body, "changed, whole new version")

    base_entry = base_by.get(path)
    base_text = read_text("base", base_entry) if base_entry is not None else None
    sub_text = read_text("sub", sub_entry)
    if base_text is None or sub_text is None:
        return _file_block("CHANGED-DIFF", path, None, "changed, diff unavailable")
    return _file_block(
        "CHANGED-DIFF",
        path,
        _unified_diff(base_text, sub_text, path),
        "changed, unified diff base -> sub",
    )


def _neighbour_paths(
    delta: Delta,
    sub_by: dict[str, ManifestEntry],
    base_by: dict[str, ManifestEntry],
    read_text: ReadText,
) -> tuple[str, ...]:
    """Unchanged base files whose basename is name-dropped by a changed file.

    One hop, no transitivity / AST. A candidate is an UNCHANGED base file
    (present in the submission, not in ``changed``). It qualifies when its
    ``basename+ext`` appears as a whole word (case-sensitive) in the
    extracted text of any changed file. Deduped, sorted. Precision over
    recall -- a substring (``utils`` inside ``myutils``) does not match.
    """
    changed = set(delta.changed)
    candidates = [
        entry
        for path, entry in base_by.items()
        if path not in changed and path in sub_by
    ]
    if not candidates:
        return ()

    by_basename: dict[str, list[str]] = {}
    for entry in candidates:
        by_basename.setdefault(_basename(entry.path), []).append(entry.path)

    hits: set[str] = set()
    for path in delta.changed:
        sub_entry = sub_by.get(path)
        if sub_entry is None:
            continue
        text = read_text("sub", sub_entry)
        if text is None:
            continue
        for basename, paths in by_basename.items():
            if _mentions(text, basename):
                hits.update(paths)
    return tuple(sorted(hits))


def _mentions(text: str, basename: str) -> bool:
    """Whole-word, case-sensitive occurrence of ``basename`` in ``text``.

    Boundaries reject word characters AND dots on either side, so a full
    filename token (``utils.py``) matches ``import utils.py`` and
    ``src/utils.py`` but not ``myutils.py``, ``utils.pyx`` or ``utils``.
    """
    pattern = r"(?<![\w.])" + re.escape(basename) + r"(?![\w.])"
    return re.search(pattern, text) is not None


# ── Trusted block (system-computed facts) ──────────────────────────────────


def _render_trusted(
    base: Manifest,
    sub: Manifest,
    delta: Delta,
    base_version: int | None,
    latest_version: int | None,
) -> str:
    sub_by = {entry.path: entry for entry in sub.included}
    base_by = {entry.path: entry for entry in base.included}

    out: list[str] = [
        f"{_SENTINEL} TRUSTED -- SYSTEM-COMPUTED metadata below, NOT student input.",
        "",
        "## Base project structure (system-computed)",
        *_render_tree(base),
        "### Excluded from base snapshot",
        *_render_excluded(base),
        "",
        "## Change summary vs base (system-computed)",
        _delta_line("CHANGED", delta.changed, sub_by),
        _delta_line("NEW", delta.new, sub_by),
        _delta_line("DELETED", delta.deleted, base_by, sized=False),
        _delta_line(
            "HYGIENE (new excluded vs base)",
            delta.hygiene_new_excluded,
            base_by,
            sized=False,
        ),
        "",
        "## Metrics (system-computed)",
        _f2_line(base, delta),
        _staleness_line(base_version, latest_version),
        f"{_SENTINEL} END-TRUSTED",
    ]
    return "\n".join(out)


def _render_tree(manifest: Manifest) -> list[str]:
    """Indented pseudo-tree of the INCLUDED entries (sorted, deterministic)."""
    if not manifest.included:
        return ["(no included files)"]
    lines: list[str] = []
    prev: list[str] = []
    for entry in sorted(manifest.included, key=lambda e: e.path):
        parts = entry.path.split("/")
        dirs, name = parts[:-1], parts[-1]
        common = 0
        while (
            common < len(prev) and common < len(dirs) and prev[common] == dirs[common]
        ):
            common += 1
        for depth in range(common, len(dirs)):
            lines.append(f"{'  ' * depth}{_inline(dirs[depth])}/")
        lines.append(
            f"{'  ' * len(dirs)}{_inline(name)}  "
            f"[{entry.cls.value}, {_human_size(entry.size)}]"
        )
        prev = dirs
    return lines


def _render_excluded(manifest: Manifest) -> list[str]:
    if not manifest.excluded:
        return ["(none)"]
    return [
        f"{_inline(ex.path)}  [{ex.reason.value}, {ex.entries} entries, "
        f"{_human_size(ex.size)}]"
        for ex in sorted(manifest.excluded, key=lambda e: e.path)
    ]


def _delta_line(
    label: str,
    paths: tuple[str, ...],
    by_path: dict[str, ManifestEntry],
    *,
    sized: bool = True,
) -> str:
    if not paths:
        return f"{label} (0): (none)"
    if sized:
        items = ", ".join(f"{_inline(p)} ({_size_of(p, by_path)})" for p in paths)
    else:
        items = ", ".join(_inline(p) for p in paths)
    return f"{label} ({len(paths)}): {items}"


def _f2_line(base: Manifest, delta: Delta) -> str:
    """F2 metric: fraction of the base rewritten / deleted (+ new count)."""
    base_n = len(base.included)
    changed_n, deleted_n, new_n = len(delta.changed), len(delta.deleted), len(delta.new)
    if base_n == 0:
        return f"F2: no base attached -- all {new_n} submission files are new."
    return (
        f"F2: base has {base_n} files; {changed_n} changed "
        f"({round(100 * changed_n / base_n)}%), {deleted_n} deleted "
        f"({round(100 * deleted_n / base_n)}%); {new_n} new files added."
    )


def _staleness_line(base_version: int | None, latest_version: int | None) -> str:
    if base_version is None:
        return "Staleness: no base attached."
    if latest_version is None or latest_version == base_version:
        return f"Staleness: built on base v{base_version} (latest ready)."
    return (
        f"Staleness: built on base v{base_version}; "
        f"latest ready is v{latest_version} -- STALE."
    )


# ── Untrusted file block + primitives ──────────────────────────────────────


def _file_block(kind: str, path: str, body: str | None, note: str) -> str:
    """One self-describing untrusted file block; body escapes the sentinel."""
    header = f"{_SENTINEL} FILE type={kind} path={_inline(path)}"
    if note:
        header += f" -- {note}"
    content = _BODY_UNAVAILABLE if body is None else _escape(body)
    return f"{header}\n{content}\n{_SENTINEL} END-FILE"


def _unified_diff(base_text: str, sub_text: str, path: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            base_text.splitlines(),
            sub_text.splitlines(),
            fromfile=f"base/{path}",
            tofile=f"sub/{path}",
            lineterm="",
        )
    )


def _escape(text: str) -> str:
    """Neutralise the structural sentinel in untrusted content (exact-match)."""
    return text.replace(_SENTINEL, _SENTINEL_ESCAPED)


def _inline(text: str) -> str:
    """Escape a value for safe use ON a single delimiter line (path, name)."""
    return _escape(text).replace("\r", "\\r").replace("\n", "\\n")


def _size_of(path: str, by_path: dict[str, ManifestEntry]) -> str:
    entry = by_path.get(path)
    return _human_size(entry.size) if entry is not None else "?"


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
