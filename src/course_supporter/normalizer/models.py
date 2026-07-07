"""Data forms for the deterministic project-archive normalizer (KD18 P1).

Pure value objects -- no extraction, classification, or orchestration
logic lives here (that arrives in later commits: classify map,
canonical-zip writer, ``normalize_archive``, ``compute_delta``). The
canonical identity primitives that derive a snapshot's
``aggregate_hash`` live in :mod:`course_supporter.normalizer.hashing`;
this module only declares the shapes those primitives populate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class EntryClass(StrEnum):
    """Content class assigned to every INCLUDED manifest entry."""

    TEXT = "text"
    """Extractable-as-text source (code, markup, config, data)."""
    DOCUMENT = "document"
    """docx / pdf -- stored as opaque bytes; text pulled at diff-time."""
    BINARY = "binary"
    """Everything else -- hash-tracked only, outside the review scope."""


class ExcludedReason(StrEnum):
    """Exhaustive reason set for an entry omitted from the snapshot.

    ``FORBIDDEN_TYPE`` from the security layer is deliberately NOT here:
    an unknown extension is kept as an :attr:`EntryClass.BINARY`
    INCLUDED entry (hash-tracked), not excluded. Only these three
    reasons drop content from the snapshot.
    """

    DENYLIST_DIR = "denylist_dir"
    """Entry sits under a denied directory (``.venv`` etc.); the whole
    directory is collapsed to a single excluded row."""
    MAGIC_MISMATCH = "magic_mismatch"
    """Whitelisted extension but content disagrees -- a security signal,
    not silent binary."""
    NESTED_ARCHIVE = "nested_archive"
    """Entry is itself an archive; never opened (bomb vector stays
    unreachable), recorded as an opaque raw-hashed blob."""


@dataclass(frozen=True, slots=True)
class NormalizerLimits:
    """Two-level limits (KD18): a raw unpack-guard and a content cap.

    Level 1 guards the raw archive against zip-bombs before denylist
    pruning. Level 2 caps the cleaned snapshot after denylist pruning (a
    forgotten ``.venv`` collapses to a manifest row rather than tripping
    the cap). ``kept_single_max_bytes`` is the single home for the
    per-file review threshold; P1 stores it but applies no filter -- P4
    compares ``ManifestEntry.size`` against it downstream.
    """

    raw_max_unzipped_bytes: int = 150 * 1024 * 1024
    raw_max_nesting_depth: int = 1
    kept_total_max_bytes: int = 50 * 1024 * 1024
    kept_single_max_bytes: int = 2 * 1024 * 1024


# ── Shared project-normalize limits (SECURITY KNOB — review-able) ──────────
# The single NormalizerLimits instance used to normalize BOTH sides of a project
# task: the author's base archive (P2 base-normalize worker) and the student's
# submission (P3 delta-submit worker). One constant, deliberately:
#
#   * Symmetric accept/reject — a submission is a superset of the base (same
#     project, evolved). An asymmetric cap (a smaller submission limit) would
#     reject valid submissions the base itself passed; base == submission keeps
#     the accept/reject envelope identical on both sides.
#   * One kept_single threshold — P4 compares ``ManifestEntry.size`` against
#     ``kept_single_max_bytes`` to pick a per-file render mode (whole vs unified
#     diff); a single value keeps that decision predictable across both sides.
#
# These limits do NOT drive inclusion/exclusion — ``ExcludedReason`` is
# exhaustive (denylist_dir / magic_mismatch / nested_archive); there is no
# size-based exclusion, and exceeding ``kept_total_max_bytes`` RAISES the whole
# archive (``NormalizerLimitError``) rather than silently dropping entries. They
# are an accept/reject anti-bomb guard + the P4 per-file threshold, NOT an
# "honest delta" mechanism.
#
#   raw_max_unzipped_bytes 200 MB — level-1 cumulative unzip guard (anti-bomb),
#       generous for a real author-curated project, far below any bomb.
#   raw_max_nesting_depth   1     — a nested archive is recorded as an excluded
#       entry, never recursed into (bomb vector stays unreachable).
#   kept_total_max_bytes  100 MB  — level-2 cap on the CLEANED snapshot, applied
#       AFTER the denylist collapse (a forgotten .venv does not count).
#   kept_single_max_bytes   5 MB  — P4 per-file review threshold; the normalizer
#       stores but does not enforce it.
_PROJECT_NORMALIZE_LIMITS: Final[NormalizerLimits] = NormalizerLimits(
    raw_max_unzipped_bytes=200 * 1024 * 1024,
    raw_max_nesting_depth=1,
    kept_total_max_bytes=100 * 1024 * 1024,
    kept_single_max_bytes=5 * 1024 * 1024,
)


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One INCLUDED file in the normalized snapshot.

    Attributes:
        path: Canonical POSIX path -- ``str(PurePosixPath(arcname))``
            (see
            :func:`course_supporter.normalizer.hashing.canonicalize_path`):
            leading ``./`` stripped, ``.`` / ``//`` segments collapsed,
            forward-slash separators, NFKC form inherited from the
            security layer. This canonical form is what keeps the
            aggregate hash stable across repackaging.
        size: Length of the raw bytes.
        hash: Lowercase hex SHA-256 of the raw bytes (64 chars). Raw --
            docx/pdf are hashed as bytes so any change is detected.
        cls: The entry's :class:`EntryClass`.
    """

    path: str
    size: int
    hash: str
    cls: EntryClass


@dataclass(frozen=True, slots=True)
class ExcludedEntry:
    """One omitted path, or one collapsed denied directory.

    Attributes:
        path: Canonical POSIX path of the file, or the denied directory
            prefix (e.g. ``a/node_modules/``) when ``reason`` is
            :attr:`ExcludedReason.DENYLIST_DIR`.
        reason: Why it was excluded.
        entries: Number of raw files this row represents (``1`` for a
            single file; the collapsed count for a denylist directory).
        size: Sum of raw bytes represented by this row.
    """

    path: str
    reason: ExcludedReason
    entries: int
    size: int


@dataclass(frozen=True, slots=True)
class Manifest:
    """Algorithmic description of a normalized archive (base or submission).

    Covers the raw content *including* what was excluded. ``included``
    is the snapshot proper; ``excluded`` records what was dropped and
    why (denylist directories collapsed to one row each).

    ``aggregate_hash`` byte-spec -- the single echo-match contract shared
    by the normalizer that writes it and the P3 matcher that compares it.
    Reproduce it exactly or the two sides diverge and echo-match fails
    silently::

        for each entry in ``included``:
            line = f"{entry.path}:{entry.hash}"
              path -- canonical: str(PurePosixPath(arcname))
              hash -- lowercase hex sha256 of raw bytes (64 chars)
        lines = sorted(all lines)       # Python default str order ==
                                        # Unicode code point; paths are
                                        # unique so this equals
                                        # sort-by-path (ASCII == byte)
        blob  = "\\n".join(lines)       # LF separator, NO trailing newline
        aggregate_hash = sha256(blob.encode("utf-8")).hexdigest()

    ``excluded`` entries do NOT contribute to ``aggregate_hash``. The
    reference implementation is
    :func:`course_supporter.normalizer.hashing.compute_aggregate_hash`.

    Attributes:
        schema: Manifest schema version (currently ``1``).
        aggregate_hash: Snapshot identity per the byte-spec above.
        included: Kept files, one :class:`ManifestEntry` each.
        excluded: Dropped content, one :class:`ExcludedEntry` each.
        total_files: Count of raw files across included + excluded.
        total_bytes: Sum of raw bytes across included + excluded.
    """

    schema: int
    aggregate_hash: str
    included: tuple[ManifestEntry, ...]
    excluded: tuple[ExcludedEntry, ...]
    total_files: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class NormalizedSnapshot:
    """Result of normalizing one archive: manifest + reproducible zip.

    Attributes:
        manifest: The algorithmic :class:`Manifest`.
        canonical_zip: Byte-reproducible zip of the INCLUDED entries
            (deterministic metadata; written by the canonical-zip writer
            in a later commit).
        snapshot_hash: Echo-match key. Equals
            ``manifest.aggregate_hash``. P3 matches a submission's
            ``base_snapshot_hash`` against this value on ANY base version
            (staleness surfaces separately).
    """

    manifest: Manifest
    canonical_zip: bytes
    snapshot_hash: str


@dataclass(frozen=True, slots=True)
class Delta:
    """Difference of a submission snapshot against a base snapshot.

    Compared by canonical ``path``: ``changed`` = same path, different
    hash; ``new`` = path only in the submission; ``deleted`` = path only
    in the base. ``hygiene_new_excluded`` is the hygiene level --
    excluded paths present in the submission but not the base (a
    forgotten ``.venv`` slipping in).
    """

    changed: tuple[str, ...]
    new: tuple[str, ...]
    deleted: tuple[str, ...]
    hygiene_new_excluded: tuple[str, ...]
