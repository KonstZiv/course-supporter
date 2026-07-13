"""Reason vocabulary for the code-material project tree (№19).

The tree the code processor persists (``DocumentSummary.structure``) and
feeds to the ``code_summary`` LLM call has THREE consumers — the DB (full
per-file persist), the LLM (``structure_block``), and, later, the author
UI (DD-CM-B). One raw string cannot be right for all three, and №19 was
exactly that failure: the security layer's ``EntryVerdict`` value flowed
verbatim into the prompt (``code.py`` ``entry.verdict.value``), telling
the model ".gitignore — forbidden_type". That name is the strict-homework
contour's, where a type really IS forbidden; in the code contour nothing
is forbidden — a ``.gitignore`` is simply not code. And ``denylist_dir``
was hard-coded onto leaf files, so a ``.DS_Store`` claimed to be a
directory whose value equalled its own path.

Fix: this module is the code contour's OWN reason vocabulary — a mirror
of the normalizer's ``ExcludedReason`` (``normalizer/models.py``), which
for the same reason deliberately refuses to carry ``FORBIDDEN_TYPE``:

* ``CodeStructureReason`` — the single enum every structure token is
  drawn from. Its values are the machine-readable tokens the DB (and the
  future UI) read.
* ``reason_for_verdict`` — the ONLY bridge from the foreign
  ``EntryVerdict`` into this vocabulary. Total over the verdicts that can
  reach the classify-mode else-branch; a new one raises rather than
  leaking its raw value into the prompt (that regression is №19). We do
  NOT rename ``EntryVerdict.FORBIDDEN_TYPE`` (truthful in the strict
  contour) — we translate it here.
* ``structure_reason`` — builds the persisted ``"<token>[: <detail>]"``
  string. It takes the ENUM, never a ``str``, so ``entry.verdict.value``
  (a ``str``) fails ``mypy`` at the call site: the no-raw-passthrough
  invariant is held by the type-checker, not by a test.

On top of that vocabulary sits the LLM presentation layer — ``describe_for_llm``
(token -> human consequence clause) and ``render_structure_block`` (the whole
``structure_block`` the ``code_summary`` call sees). It gives the model the
CONSEQUENCE, not the mechanism, and AGGREGATES the excluded set to one line
per kind with counts, so the prompt is not flooded with hundreds of
``__MACOSX/._*`` paths. The DB keeps the full per-file tree; this collapse
lives only in the prompt (the three consumers, spoken to differently).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Final

from course_supporter.security.archive import EntryVerdict


class CodeStructureReason(StrEnum):
    """Every reason token the code-material structure can carry (№19).

    Values are the machine-readable tokens persisted in
    ``DocumentSummary.structure``. ``non_code_type`` replaces the leaked
    ``forbidden_type``; ``denylist_dir`` / ``denylist_file`` split the
    once-hard-coded token so it describes what the object IS.
    """

    DENYLIST_DIR = "denylist_dir"
    DENYLIST_FILE = "denylist_file"
    NON_CODE_TYPE = "non_code_type"
    MAGIC_MISMATCH = "magic_mismatch"
    NESTED_ARCHIVE = "nested_archive"
    VENDORED_DIR = "vendored_dir"
    LOCKFILE = "lockfile"
    GENERATED_ARTIFACT = "generated_artifact"
    OVERSIZE = "oversize"


# The ONLY bridge from the security layer's EntryVerdict into the code
# contour's own vocabulary. Total over the three verdicts that reach the
# classify-mode else-branch (INCLUDED / DENYLIST_SKIP are handled by the
# caller before this map). FORBIDDEN_TYPE -> NON_CODE_TYPE is the №19
# truth-fix: EntryVerdict.FORBIDDEN_TYPE stays named as it is (it is
# truthful in the strict-homework contour) — we translate, never rename.
_VERDICT_TO_REASON: Final[dict[EntryVerdict, CodeStructureReason]] = {
    EntryVerdict.FORBIDDEN_TYPE: CodeStructureReason.NON_CODE_TYPE,
    EntryVerdict.MAGIC_MISMATCH: CodeStructureReason.MAGIC_MISMATCH,
    EntryVerdict.NESTED_ARCHIVE: CodeStructureReason.NESTED_ARCHIVE,
}


def reason_for_verdict(verdict: EntryVerdict) -> CodeStructureReason:
    """Translate a classify-mode ``EntryVerdict`` into a structure token.

    Raises on ``INCLUDED`` / ``DENYLIST_SKIP`` (handled by the caller) or
    any unmapped verdict — a loud impossible-state guard so a new
    ``EntryVerdict`` member can never silently leak its raw value into the
    prompt again (that regression is №19).
    """
    reason = _VERDICT_TO_REASON.get(verdict)
    if reason is None:
        raise ValueError(
            f"no code-structure reason mapped for verdict {verdict!r}; extend "
            "_VERDICT_TO_REASON (a raw EntryVerdict.value must never reach "
            "DocumentSummary.structure — that is №19)"
        )
    return reason


def denylist_token(prefix: str) -> CodeStructureReason:
    """DIR vs FILE from the ``denylist_prefix`` shape.

    ``denylist_prefix`` marks a directory collapse with a trailing ``/``
    and a denied leaf file (``.DS_Store``) by its absence. The token must
    describe what the object IS — a leaf is not a directory (№19).
    """
    return (
        CodeStructureReason.DENYLIST_DIR
        if prefix.endswith("/")
        else CodeStructureReason.DENYLIST_FILE
    )


def structure_reason(reason: CodeStructureReason, detail: str | None = None) -> str:
    """Build the persisted ``"<token>[: <detail>]"`` reason string.

    Takes the ENUM, never a ``str``: passing ``entry.verdict.value`` (a
    ``str``) fails ``mypy`` here, so №19's no-raw-passthrough invariant is
    enforced by the type-checker rather than a test. ``detail`` is omitted
    for tokens whose ``path`` already carries everything (denylist rows,
    the else-branch verdicts); the typicality layer passes the matched
    pattern as ``detail``.
    """
    return f"{reason.value}: {detail}" if detail is not None else reason.value


# ── LLM layer: consequence, not mechanism (ratified C + D1) ────────────

# Token -> human consequence clause (lowercase; the renderer capitalises
# at sentence start). The model must understand what it was NOT given and
# why that is not a loss — never the internal token, never a raw verdict.
_LLM_PHRASE: Final[dict[CodeStructureReason, str]] = {
    CodeStructureReason.DENYLIST_DIR: (
        "службова тека середовища/редактора — не частина матеріалу"
    ),
    CodeStructureReason.DENYLIST_FILE: (
        "службовий файл середовища — не частина матеріалу"
    ),
    CodeStructureReason.NON_CODE_TYPE: "не є кодом",
    CodeStructureReason.MAGIC_MISMATCH: "вміст не відповідає розширенню — пропущено",
    CodeStructureReason.NESTED_ARCHIVE: "вкладений архів — не відкрито",
    CodeStructureReason.VENDORED_DIR: "стороння бібліотека — подано лише описом",
    CodeStructureReason.LOCKFILE: "залежності проєкту — подано лише описом",
    CodeStructureReason.GENERATED_ARTIFACT: (
        "згенерований артефакт — подано лише описом"
    ),
    CodeStructureReason.OVERSIZE: "завеликий файл — подано лише описом",
}

# Tokens that can appear on an ``excluded`` structure row. The remaining
# four (vendored_dir / lockfile / generated_artifact / oversize) are
# produced only by the typicality layer, which always yields
# ``description_only`` — never ``excluded``. _aggregate_excluded guards
# this loudly: a description-only token reaching the excluded set would
# otherwise vanish from the prompt with no error (a silent drop — the very
# class of bug this module exists to kill).
_EXCLUDED_TOKENS: Final[frozenset[CodeStructureReason]] = frozenset(
    {
        CodeStructureReason.DENYLIST_DIR,
        CodeStructureReason.DENYLIST_FILE,
        CodeStructureReason.NON_CODE_TYPE,
        CodeStructureReason.MAGIC_MISMATCH,
        CodeStructureReason.NESTED_ARCHIVE,
    }
)


def describe_for_llm(reason: CodeStructureReason) -> str:
    """Token -> human consequence clause for the ``code_summary`` prompt.

    The single explicit token->phrase mapping (ratified C). Callers add
    counts / names around it; this returns the bare clause. Deliberately
    contains no machine token substring, so the aggregated block cannot
    leak one.
    """
    return _LLM_PHRASE[reason]


def _reason_token(reason: str) -> CodeStructureReason:
    """Recover the enum token from a persisted reason string.

    Excluded rows carry a bare token; typicality (description-only) rows
    carry ``"<token>: <detail>"``. We only ever parse strings that
    :func:`structure_reason` produced, so the split is deterministic.
    """
    return CodeStructureReason(reason.split(":", 1)[0].strip())


def _cap(clause: str) -> str:
    return clause[:1].upper() + clause[1:] if clause else clause


# Cap on distinct non-code basenames named in one aggregated line — enough
# for the model to recognise the kind (.gitignore, favicon.ico, …) without
# turning the count line back into a path list.
_MAX_DISTINCT_BASENAMES: Final[int] = 5


def _distinct_basenames(
    rows: Sequence[Mapping[str, Any]], limit: int = _MAX_DISTINCT_BASENAMES
) -> str:
    names = sorted({PurePosixPath(str(r["path"])).name for r in rows})
    head = ", ".join(names[:limit])
    return head + ", …" if len(names) > limit else head


def render_structure_block(entries: Sequence[Mapping[str, Any]]) -> str:
    """Render the ``structure_block`` the ``code_summary`` LLM call sees.

    Three layers made concrete (D1): ``included`` and ``description_only``
    stay PER-FILE (the model must see ``package-lock.json`` by name — it
    is a project dependency), while ``excluded`` junk is AGGREGATED to one
    line per kind with counts. The persisted DB tree stays full; this
    collapse lives only in the prompt so the model gets the fact, not
    hundreds of AppleDouble paths.
    """
    included: list[str] = []
    description: list[str] = []
    excluded: dict[CodeStructureReason, list[Mapping[str, Any]]] = defaultdict(list)

    for entry in entries:
        cls = entry["cls"]
        path = str(entry["path"])
        size = int(entry["size"])
        if cls == "included":
            included.append(f"{path} ({size} B)")
        elif cls == "description_only":
            clause = describe_for_llm(_reason_token(str(entry["reason"])))
            description.append(f"{path} ({size} B) — {clause}")
        elif cls == "excluded":
            excluded[_reason_token(str(entry["reason"]))].append(entry)

    lines = [*included, *description, *_aggregate_excluded(excluded)]
    return "\n".join(lines)


def _aggregate_excluded(
    groups: Mapping[CodeStructureReason, Sequence[Mapping[str, Any]]],
) -> list[str]:
    """One consequence line per excluded kind (D1) — counts, never paths.

    Service/environment junk (denylist dirs + leaf files) collapses into a
    single line with directory and file counts; ``non_code_type`` lists
    distinct basenames (they are informative — ``.gitignore`` tells the
    model the project uses git); ``magic_mismatch`` / ``nested_archive``
    report a count. ``entries`` on a collapsed denylist-dir row is the
    number of files it stands for.
    """
    unexpected = sorted(
        r.value for r, rows in groups.items() if rows and r not in _EXCLUDED_TOKENS
    )
    if unexpected:
        raise ValueError(
            f"excluded structure rows carry non-excluded reasons {unexpected}; "
            "these tokens are description_only-only — routing one to excluded "
            "would silently drop it from the prompt (loud guard, sibling of "
            "reason_for_verdict)"
        )

    lines: list[str] = []

    dirs = groups.get(CodeStructureReason.DENYLIST_DIR, ())
    files = groups.get(CodeStructureReason.DENYLIST_FILE, ())
    if dirs or files:
        n_files = sum(int(r.get("entries", 1)) for r in dirs) + len(files)
        parts = []
        if dirs:
            parts.append(f"тек: {len(dirs)}")
        parts.append(f"файлів: {n_files}")
        lines.append(
            "Службові теки та файли середовища/редактора — "
            f"пропущено ({', '.join(parts)})"
        )

    non_code = groups.get(CodeStructureReason.NON_CODE_TYPE, ())
    if non_code:
        clause = _cap(describe_for_llm(CodeStructureReason.NON_CODE_TYPE))
        lines.append(
            f"{clause} — пропущено (файлів: {len(non_code)}; "
            f"{_distinct_basenames(non_code)})"
        )

    magic = groups.get(CodeStructureReason.MAGIC_MISMATCH, ())
    if magic:
        clause = _cap(describe_for_llm(CodeStructureReason.MAGIC_MISMATCH))
        lines.append(f"{clause} (файлів: {len(magic)})")

    nested = groups.get(CodeStructureReason.NESTED_ARCHIVE, ())
    if nested:
        clause = _cap(describe_for_llm(CodeStructureReason.NESTED_ARCHIVE))
        lines.append(f"{clause} (шт.: {len(nested)})")

    return lines


__all__ = [
    "CodeStructureReason",
    "denylist_token",
    "describe_for_llm",
    "reason_for_verdict",
    "render_structure_block",
    "structure_reason",
]
