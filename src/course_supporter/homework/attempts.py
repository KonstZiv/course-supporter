"""Which work an attempt carries, and what changed since the last one (task 13, K1).

Purpose:
    A student may send the same task again. The new Mentor is to know which
    attempt it is reading, what changed since the previous one, and not to pay
    twice for the same work. Every one of those rests on two questions, asked
    of every task type through one port: what is the work of this attempt — a
    fingerprint that is equal exactly when the work is the same
    (:meth:`AttemptSnapshotter.snapshot`) — and what changed between two
    attempts (:meth:`AttemptSnapshotter.diff`). Whether an equal fingerprint
    may spare the paid review is the adapter's own answer
    (:attr:`AttemptSnapshotter.dedup_enabled`).

    Nothing calls the port yet: the attempt ledger (K2), the dedup at the
    doors (K3), the difference handed to the evaluation (K4) and the attempt
    number in the API (K5) come later. The dedup by the raw file hash
    (``file_hash``) that exists today is a different thing and is not touched.

Input and output:
    :meth:`AttemptSnapshotter.snapshot` takes an :class:`AttemptWork` — what
    the doors read (:class:`~course_supporter.homework.doors.DoorReading`) and,
    for a project, the snapshot the normalizer stored on the submission — and
    returns an :class:`AttemptSnapshot`: the kind, the fingerprint (SHA-256
    hex) and the body both the fingerprint and the difference are computed
    from. :meth:`AttemptSnapshotter.diff` takes two snapshots of its own kind,
    the previous one first, and returns an :class:`AttemptDiff`; the same work
    gives an empty one (:attr:`AttemptDiff.is_empty`). A snapshot of another
    kind is a ``ValueError`` — the caller's mistake, never the student's.

The same work — after normalization, of what the model sees:
    * ``text`` (``short_task`` and ``task``; a file or an archive) —
      :class:`TextAttemptSnapshotter`. The input is
      :attr:`~course_supporter.homework.doors.DoorReading.safety_text`: the
      student's files, their encoding already recovered by the doors, without
      the block the doors append naming what was not opened — a file left
      unread is the platform's line, not the student's work.
      :func:`normalize_work_text` drops a leading BOM, turns every line end
      into LF and composes Unicode to NFC; spaces inside a line, indentation
      and empty lines stay as they are, because in code they are the work.
      An archive's text lays its files out in the order of the zip
      (``Stage1Result.archive_entries``, ``text_budget.fit_archive_entries``),
      so :func:`normalized_work_text` puts its frames in order of path first.
      One function serves the fingerprint and the difference.
    * ``project`` — :class:`ProjectAttemptSnapshotter`. The fingerprint IS the
      ``snapshot_hash`` the normalizer stored (KD18), never computed again;
      the difference is the existing
      :func:`~course_supporter.normalizer.compute_delta` of the two manifests.
      Known limitation: the snapshot hashes each file's raw bytes, so the same
      project saved in another encoding or with other line ends is another
      work. Normalizing a project's files is deferred to the project chain.
    * ``test`` — :class:`TestAttemptSnapshotter`. The fingerprint is of the
      canonical record of the answers
      (:func:`~course_supporter.homework.test_doors.stored_answers`: the
      version and the answers, keys sorted, no whitespace), read back and
      written again so that the same answers in another key order are one
      record. Every attempt of a test is scored anew (task 07), so
      ``dedup_enabled`` is ``False`` and the difference is always empty.

Replacing:
    Anything with ``snapshot(work)``, ``diff(previous, current)`` and a
    ``dedup_enabled`` property of these signatures satisfies
    :class:`AttemptSnapshotter` — a structural ``Protocol``, no base class to
    inherit — and is used where an adapter of this module is used; no caller
    changes. A project adapter that normalizes file contents (the limitation
    above), for example, hashes the normalized texts of the canonical snapshot
    for its fingerprint and keeps ``snapshot_hash`` in the body beside them;
    a type whose attempts should no longer be deduplicated answers ``False``
    from ``dedup_enabled`` and keeps its fingerprint for the ledger.

    The production choice is made in one place, :func:`attempt_snapshotter_for`:
    return the new adapter there for its type. Tests of other modules
    substitute that factory, so they keep working whichever adapter it returns.

    Worked cases, executed: :mod:`tests.unit.test_homework.test_attempts`.
"""

from __future__ import annotations

import difflib
import hashlib
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from course_supporter.homework.doors import DoorReading
from course_supporter.homework.test_doors import read_stored_answers, stored_answers
from course_supporter.homework.test_text import canonical_answers

# The frame the doors render around each archive member: read from the one
# place that renders it, so the order of frames is restored on the very shape
# the doors produced and cannot drift from it.
from course_supporter.homework.text_budget import _ENTRY_FRAME
from course_supporter.models.source import AssignmentType
from course_supporter.normalizer import Delta, Manifest, compute_delta

if TYPE_CHECKING:
    from course_supporter.homework.criteria_verdicts import WorkFile

__all__ = [
    "AttemptDiff",
    "AttemptKind",
    "AttemptSnapshot",
    "AttemptSnapshotter",
    "AttemptWork",
    "ProjectAttemptSnapshotter",
    "ProjectBody",
    "TestAttemptSnapshotter",
    "TextAttemptSnapshotter",
    "attempt_snapshotter_for",
    "normalize_work_text",
    "normalized_work_text",
]

_BOM: Final = "\ufeff"


class AttemptKind(StrEnum):
    """How an attempt's work is compared — one adapter per member.

    * ``TEXT`` — the text the doors read (``short_task``, ``task``).
    * ``TEST`` — the canonical record of the answers.
    * ``PROJECT`` — the normalizer's snapshot of the archive.
    """

    TEXT = "text"
    TEST = "test"
    PROJECT = "project"


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptWork:
    """What one attempt hands the snapshotter.

    Keyword-only: the project's two fields are optional, and a positional call
    could put one where the other belongs.

    Attributes:
        reading: What the doors read out of the submission.
        snapshot_hash: A project's ``HomeworkSubmission.snapshot_hash``;
            ``None`` for every other type.
        manifest: A project's snapshot manifest
            (``manifest_from_jsonb(HomeworkSubmission.snapshot_manifest)``);
            ``None`` for every other type.
    """

    reading: DoorReading
    snapshot_hash: str | None = None
    manifest: Manifest | None = None


@dataclass(frozen=True, slots=True)
class ProjectBody:
    """A project attempt's body: the stored snapshot and its manifest."""

    snapshot_hash: str
    manifest: Manifest


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    """One attempt's work, as compared with another.

    Attributes:
        kind: Which adapter made it, and so which can compare it.
        fingerprint: SHA-256 hex; equal exactly when the work is the same.
        body: What the fingerprint and the difference are computed from — the
            normalized text (``TEXT``), the canonical JSON of the answers
            (``TEST``), the snapshot hash and manifest (``PROJECT``).
    """

    kind: AttemptKind
    fingerprint: str
    body: str | ProjectBody


@dataclass(frozen=True, slots=True)
class AttemptDiff:
    """What changed from the previous attempt to this one.

    Attributes:
        kind: The kind of the two snapshots compared.
        unified_diff: ``TEXT``: the unified diff of the normalized texts,
            ``previous`` against ``current``; empty for the same work.
        lines_added: ``TEXT``: lines only in the current attempt.
        lines_removed: ``TEXT``: lines only in the previous attempt.
        project_delta: ``PROJECT``: the existing path-level difference
            (:func:`~course_supporter.normalizer.compute_delta`, the previous
            manifest as its base) — which files, the counts below are its
            sizes; ``None`` for the other kinds. A ``TEST`` difference carries
            nothing.
    """

    kind: AttemptKind
    unified_diff: str = ""
    lines_added: int = 0
    lines_removed: int = 0
    project_delta: Delta | None = None

    @property
    def files_changed(self) -> int:
        """``PROJECT``: files in both attempts whose content differs."""
        return len(self.project_delta.changed) if self.project_delta else 0

    @property
    def files_added(self) -> int:
        """``PROJECT``: files only in the current attempt."""
        return len(self.project_delta.new) if self.project_delta else 0

    @property
    def files_removed(self) -> int:
        """``PROJECT``: files only in the previous attempt."""
        return len(self.project_delta.deleted) if self.project_delta else 0

    @property
    def is_empty(self) -> bool:
        """Nothing of the work changed.

        A project's ``hygiene_new_excluded`` is not the work — the snapshot
        hash does not cover excluded files either.
        """
        return not (
            self.unified_diff
            or self.files_changed
            or self.files_added
            or self.files_removed
        )


class AttemptSnapshotter(Protocol):
    """The replaceable port; the module docstring states the contract."""

    @property
    def dedup_enabled(self) -> bool:
        """Whether an attempt with the previous one's fingerprint may skip review."""
        ...

    def snapshot(self, work: AttemptWork) -> AttemptSnapshot:
        """The attempt's work: its fingerprint and the body it is computed from."""
        ...

    def diff(self, previous: AttemptSnapshot, current: AttemptSnapshot) -> AttemptDiff:
        """What changed from ``previous`` to ``current``; empty for the same work."""
        ...


def normalize_work_text(text: str) -> str:
    """The student's text with only what the encoding put there taken off.

    A leading BOM is dropped, every line end becomes LF (CRLF and a lone CR
    alike), and the text is composed to NFC. Nothing else is touched: spaces
    inside a line, indentation and empty lines are part of the work.
    """
    text = text.removeprefix(_BOM).replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text)


def normalized_work_text(reading: DoorReading) -> str:
    """What a text attempt is compared by: the safety text, normalized.

    An archive's text is its files in frames, in the order of the zip. When
    the text is exactly the frames of :attr:`DoorReading.files` — the shape the
    doors render — the files are laid out again in order of path, each body
    normalized on its own (a BOM opens each file, not the text). Anything else
    — a single file, a document, or a reading carried without its files
    (today's Mentor builds one so) — is normalized as one text, in the order
    it came.
    """
    text = reading.safety_text
    if reading.files and text == _framed(
        (file.name, file.text) for file in reading.files
    ):
        ordered = sorted(reading.files, key=_by_path)
        return _framed(
            (normalize_work_text(file.name), normalize_work_text(file.text))
            for file in ordered
        )
    return normalize_work_text(text)


def _framed(files: Iterable[tuple[str, str]]) -> str:
    """Files in frames, joined as ``text_budget.fit_archive_entries`` joins them."""
    return "\n".join(_ENTRY_FRAME.format(name=name) + body for name, body in files)


def _by_path(file: WorkFile) -> tuple[str, str]:
    return file.name, file.text


class TextAttemptSnapshotter:
    """``short_task`` and ``task``: the normalized text the doors read."""

    @property
    def dedup_enabled(self) -> bool:
        return True

    def snapshot(self, work: AttemptWork) -> AttemptSnapshot:
        body = normalized_work_text(work.reading)
        return AttemptSnapshot(AttemptKind.TEXT, _sha256(body), body)

    def diff(self, previous: AttemptSnapshot, current: AttemptSnapshot) -> AttemptDiff:
        before = _text_body(previous, AttemptKind.TEXT)
        after = _text_body(current, AttemptKind.TEXT)
        if before == after:
            return AttemptDiff(AttemptKind.TEXT)
        return _text_diff(before, after)


class ProjectAttemptSnapshotter:
    """``project``: the snapshot the normalizer stored, compared by manifest."""

    @property
    def dedup_enabled(self) -> bool:
        return True

    def snapshot(self, work: AttemptWork) -> AttemptSnapshot:
        if work.snapshot_hash is None or work.manifest is None:
            msg = "a project attempt needs the snapshot hash and manifest stored for it"
            raise ValueError(msg)
        if work.manifest.aggregate_hash != work.snapshot_hash:
            msg = "the manifest given is not the snapshot the hash names"
            raise ValueError(msg)
        return AttemptSnapshot(
            AttemptKind.PROJECT,
            work.snapshot_hash,
            ProjectBody(work.snapshot_hash, work.manifest),
        )

    def diff(self, previous: AttemptSnapshot, current: AttemptSnapshot) -> AttemptDiff:
        before = _project_body(previous)
        after = _project_body(current)
        return AttemptDiff(
            AttemptKind.PROJECT,
            project_delta=compute_delta(before.manifest, after.manifest),
        )


class TestAttemptSnapshotter:
    """``test``: the canonical record of the answers; never deduplicated."""

    __test__ = False  # an adapter, not a pytest class, whatever its name says

    @property
    def dedup_enabled(self) -> bool:
        return False

    def snapshot(self, work: AttemptWork) -> AttemptSnapshot:
        version_id, answers = read_stored_answers(work.reading.text)
        body = stored_answers(version_id, canonical_answers(answers))
        return AttemptSnapshot(AttemptKind.TEST, _sha256(body), body)

    def diff(self, previous: AttemptSnapshot, current: AttemptSnapshot) -> AttemptDiff:
        _text_body(previous, AttemptKind.TEST)
        _text_body(current, AttemptKind.TEST)
        return AttemptDiff(AttemptKind.TEST)


_TEXT: Final = TextAttemptSnapshotter()

_SNAPSHOTTERS: Final[Mapping[AssignmentType, AttemptSnapshotter]] = {
    AssignmentType.TEST: TestAttemptSnapshotter(),
    AssignmentType.SHORT_TASK: _TEXT,
    AssignmentType.TASK: _TEXT,
    AssignmentType.PROJECT: ProjectAttemptSnapshotter(),
}


def attempt_snapshotter_for(task_type: str) -> AttemptSnapshotter:
    """The adapter of a task type — the one place the production choice is made.

    Raises:
        ValueError: ``task_type`` is not an :class:`AssignmentType`.
    """
    return _SNAPSHOTTERS[AssignmentType(task_type)]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text_body(snapshot: AttemptSnapshot, kind: AttemptKind) -> str:
    _require_kind(snapshot, kind)
    if not isinstance(snapshot.body, str):
        msg = f"a {kind} snapshot carries text, not {type(snapshot.body).__name__}"
        raise ValueError(msg)
    return snapshot.body


def _project_body(snapshot: AttemptSnapshot) -> ProjectBody:
    _require_kind(snapshot, AttemptKind.PROJECT)
    if not isinstance(snapshot.body, ProjectBody):
        msg = "a project snapshot carries its snapshot hash and manifest"
        raise ValueError(msg)
    return snapshot.body


def _require_kind(snapshot: AttemptSnapshot, kind: AttemptKind) -> None:
    if snapshot.kind is not kind:
        msg = f"a {kind} snapshotter cannot compare a {snapshot.kind} attempt"
        raise ValueError(msg)


def _text_diff(before: str, after: str) -> AttemptDiff:
    """The unified diff of two normalized texts, and its line counts.

    Both come from the same matcher with the same arguments ``unified_diff``
    uses, so the counts are the ``+`` and ``-`` lines of the diff shown. A last
    line without its LF is marked the way ``diff`` marks it, so a change of the
    final line end alone is still a visible change.
    """
    old, new = _lines(before), _lines(after)
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag in {"replace", "delete"}:
            removed += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    unified = "".join(
        line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n"
        for line in difflib.unified_diff(old, new, "previous", "current")
    )
    return AttemptDiff(
        AttemptKind.TEXT,
        unified_diff=unified,
        lines_added=added,
        lines_removed=removed,
    )


def _lines(text: str) -> list[str]:
    """``text`` cut at LF only, each line keeping its end.

    Not ``str.splitlines``: it also cuts at a form feed, a vertical tab and
    U+2028, which are characters of a line, not ends of one.
    """
    *whole, tail = text.split("\n")
    lines = [f"{line}\n" for line in whole]
    if tail:
        lines.append(tail)
    return lines
