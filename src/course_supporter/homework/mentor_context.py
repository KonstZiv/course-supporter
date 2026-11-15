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

No budget here: size is bounded BEFORE assembly, by the ``over_budget``
refusal on the project branch (homework/project_submission.py). The assembly
used to carry its own 512 KiB cap and drop overflowing units whole; step E
made that unreachable -- the pre-call refusal fires at a smaller number for
every alphabet -- so it was removed rather than left as a dead branch.

The marks of the author's lines (project chain K5) are a second, separate
pass over the assembled text, for the new path's evaluation of a project
alone: :func:`mark_authors_lines`. The text built here -- what the doors hand
on, what today's Mentor reads -- never carries them.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping
from typing import Final, Literal

from course_supporter.normalizer import Delta, Manifest, ManifestEntry

# ── Tuning constants (calibration candidates) ──────────────────────────────
# A CHANGED file at or below this raw (submission-side) size is rendered whole
# with a marker; a larger one is rendered as a unified diff (stdlib difflib).
# This is P4's OWN threshold. The normalizer has no per-file limit to confuse
# it with: the reserved ``kept_single_max_bytes`` knob that once invited the
# confusion was removed in step E, unused.
H_C_WHOLE_MAX_BYTES: Final[int] = 64 * 1024


Side = Literal["base", "sub"]
ReadText = Callable[[Side, ManifestEntry], str | None]

# Structural sentinel. EVERY delimiter line contains it, so escaping it in an
# untrusted body (exact-match) makes it impossible for student content to forge
# a boundary and break out of its slot. The escaped form does not itself
# contain the sentinel, so escaping is idempotent and non-recursive.
_SENTINEL: Final[str] = "@@MENTOR_CTX@@"
_SENTINEL_ESCAPED: Final[str] = "@@MENTOR_CTX_ESCAPED@@"
_BODY_UNAVAILABLE: Final[str] = "(body unavailable -- binary or non-extractable)"

# A changed file shown whole: its block's kind and the note on its header.
_CHANGED_FULL: Final[str] = "CHANGED-FULL"
_CHANGED_FULL_NOTE: Final[str] = "changed, whole new version"

# The marks of a whole changed file's lines (project chain K5) -- the ones a
# unified diff's lines begin with, so the model reads one convention in both.
STUDENT_LINE: Final[str] = "+"
"""A line the student added or changed."""
AUTHOR_LINE: Final[str] = " "
"""A line of the author's, as the base has it."""


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
        priority-ordered untrusted file blocks. Unbounded here on purpose --
        size is limited before assembly, by the ``over_budget`` refusal on
        the project branch.
    """
    sub_by = {entry.path: entry for entry in sub_manifest.included}
    base_by = {entry.path: entry for entry in base_manifest.included}

    trusted = _render_trusted(
        base_manifest, sub_manifest, delta, base_version, latest_version
    )
    units = _ordered_units(delta, sub_by, base_by, read_text)
    blocks = [block for _kind, _entry, block in units]
    parts = [f"{_SENTINEL} BEGIN", trusted, *blocks, f"{_SENTINEL} END"]
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
        return _file_block(_CHANGED_FULL, path, body, _CHANGED_FULL_NOTE)

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

    head = f"{_SENTINEL} TRUSTED -- SYSTEM-COMPUTED metadata below, NOT student input."

    if not base.included and not base.excluded:
        # No base attached. Every base-shaped line degenerates: the tree reads
        # "(no included files)", the exclusions "(none)", three of the four
        # delta lines "(0): (none)", and both metrics say "no base attached" in
        # more words. Printing them made the block open with three headings
        # about a base that does not exist -- the model reads those before it
        # reads anything true. One line says it instead, and the NEW list stays
        # because it is the submission's own inventory, not a comparison.
        return "\n".join(
            [
                head,
                "",
                "No base project: the whole submission is new.",
                "",
                _delta_line("NEW", delta.new, sub_by),
                f"{_SENTINEL} END-TRUSTED",
            ]
        )

    out: list[str] = [
        head,
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
    header = _file_header(kind, path, note)
    content = _BODY_UNAVAILABLE if body is None else _escape(body)
    return f"{header}\n{content}\n{_SENTINEL} END-FILE"


def _file_header(kind: str, path: str, note: str) -> str:
    """The delimiter line a file block opens with."""
    header = f"{_SENTINEL} FILE type={kind} path={_inline(path)}"
    if note:
        header += f" -- {note}"
    return header


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


def students_lines(base_text: str, sub_text: str) -> str:
    """The lines the student added or changed in a file of the base, alone.

    The ``+`` lines of the very unified diff a ``CHANGED-DIFF`` block shows
    (:func:`_unified_diff`), each without its mark and ended by a newline:
    no file header, no hunk header, no line of the author's -- neither the
    context lines nor the removed ones. For the safety check of a project on
    the new path (chain K6), which reads the student's code alone; nothing
    here reaches the context the reviewing models read.

    >>> students_lines("a\\nb\\nc\\n", "a\\nB\\nc\\nd\\n")
    'B\\nd\\n'
    >>> students_lines("a\\nb\\n", "a\\n")
    ''
    """
    # The first two lines of a non-empty diff are its file headers (``---``,
    # ``+++``); after them a line opening with ``+`` is the student's.
    diff = _unified_diff(base_text, sub_text, "").split("\n")[2:]
    return "".join(f"{line[1:]}\n" for line in diff if line.startswith("+"))


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


# ── The marks of the author's lines (project chain K5) ─────────────────────


def mark_authors_lines(
    context: str,
    *,
    student: Mapping[str, str],
    base: Mapping[str, str],
    budget_chars: int | None,
) -> str:
    """The context with every line of a whole changed file marked, or as it is.

    For the new path's evaluation of a project (chain K5, variant A of
    decision 9). A ``CHANGED-FULL`` block shows the student's new version of
    a file of the base whole; here each line of its body gets one mark by
    the difference with the base's version of the same file:
    :data:`STUDENT_LINE` for a line added or changed, :data:`AUTHOR_LINE` for
    the author's line left as it was -- the convention of a ``CHANGED-DIFF``
    block, found by the same matcher (:mod:`difflib`) on lines without their
    line ending. Frame lines are never marked, and a body keeps its escaped
    sentinel: no marked line starts with the sentinel either.

    A block is marked only when its file has a text on both sides
    (``student`` and ``base``, by path) and its body is that student text as
    the builder escaped it; any other block stays as it is.

    All or nothing: when the marked context is longer than ``budget_chars``
    -- the input budget of the project's new path -- or no budget is known,
    the context comes back byte for byte as it was given, without a single
    mark. A mark costs a character a line, and the budget is never loosened
    for it.

    Args:
        context: What :func:`build_mentor_context` assembled, with whatever
            the door appended after it.
        student: The student's files' texts, by path, as the doors read them
            (``DoorReading.files``).
        base: The base's files' texts, by path (``DoorReading.base_files``).
        budget_chars: The most characters the marked context may have.

    >>> ctx = _file_block(_CHANGED_FULL, "a.py", "x\\ny\\n", _CHANGED_FULL_NOTE)
    >>> marked = mark_authors_lines(
    ...     ctx, student={"a.py": "x\\ny\\n"}, base={"a.py": "x\\n"},
    ...     budget_chars=10_000,
    ... )
    >>> marked.split("\\n")[1:]
    [' x', '+y', '', '@@MENTOR_CTX@@ END-FILE']
    >>> mark_authors_lines(
    ...     ctx, student={"a.py": "x\\ny\\n"}, base={"a.py": "x\\n"},
    ...     budget_chars=len(marked) - 1,
    ... ) == ctx
    True
    """
    if budget_chars is None:
        return context
    headers = {
        _file_header(_CHANGED_FULL, path, _CHANGED_FULL_NOTE): path
        for path in student.keys() & base.keys()
    }
    lines = context.split("\n")
    out: list[str] = []
    at = 0
    while at < len(lines):
        line = lines[at]
        out.append(line)
        at += 1
        path = headers.get(line)
        if path is None:
            continue
        # The body runs to the next frame: no line of an escaped body starts
        # with the sentinel, so the first one that does is its END-FILE.
        end = at
        while end < len(lines) and not lines[end].startswith(_SENTINEL):
            end += 1
        out.extend(_marked_body(lines[at:end], student[path], base[path]))
        at = end
    marked = "\n".join(out)
    return marked if len(marked) <= budget_chars else context


def _marked_body(body: list[str], student_text: str, base_text: str) -> list[str]:
    """The body's lines, each with its mark; as they are when not the file's."""
    if "\n".join(body) != _escape(student_text):
        return body
    marks = _line_marks(_lines(base_text), _lines(student_text))
    # The escape adds no line, so the body's lines are the file's, in order;
    # after them only the empty piece a final newline leaves, unmarked.
    return [mark + line for mark, line in zip(marks, body, strict=False)] + body[
        len(marks) :
    ]


def _lines(text: str) -> list[str]:
    """A text's lines: each piece ended by a newline, and an unended last one."""
    pieces = text.split("\n")
    if pieces[-1] == "":
        pieces.pop()
    return pieces


def _line_marks(base: list[str], student: list[str]) -> list[str]:
    """One mark for each student line: the author's when it matches the base's."""
    matcher = difflib.SequenceMatcher(
        None,
        [line.removesuffix("\r") for line in base],
        [line.removesuffix("\r") for line in student],
    )
    marks = [STUDENT_LINE] * len(student)
    for block in matcher.get_matching_blocks():
        marks[block.b : block.b + block.size] = [AUTHOR_LINE] * block.size
    return marks
