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

The LLM presentation layer (consequence, not mechanism; excluded-set
aggregation) is added on top of this vocabulary in a later commit.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

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


__all__ = [
    "CodeStructureReason",
    "denylist_token",
    "reason_for_verdict",
    "structure_reason",
]
