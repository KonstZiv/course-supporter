"""Orchestrator for deterministic archive normalization (KD18 P1).

``normalize_archive`` is the one archive-I/O entry point: it drives the
sandbox extraction (``extract_archive_safely`` in classify mode), applies
the denylist and classification, and assembles a :class:`NormalizedSnapshot`
(algorithmic manifest + byte-reproducible zip + aggregate hash). Fully
deterministic, zero LLM.

``archive_kind`` is caller-supplied (from the upload filename extension),
mirroring ``extract_archive_safely`` which deliberately does not
autodetect: a docx's magic is a zip, so byte-autodetection cannot tell a
bare document from a project archive. Per KD18 the top-level input is
always an archive (single-file submissions are rejected upstream), so no
in-library docx/pdf guard exists here.
"""

from __future__ import annotations

from typing import Final, Literal, assert_never

from course_supporter.normalizer.canonical_zip import write_canonical_zip
from course_supporter.normalizer.classify import (
    KNOWN_EXTENSIONS,
    ExtensionClassifier,
    collapse_denylist,
    denylist_prefix,
)
from course_supporter.normalizer.exceptions import (
    NormalizerError,
    NormalizerLimitError,
)
from course_supporter.normalizer.hashing import (
    canonicalize_path,
    compute_aggregate_hash,
)
from course_supporter.normalizer.models import (
    ExcludedEntry,
    ExcludedReason,
    Manifest,
    ManifestEntry,
    NormalizedSnapshot,
    NormalizerLimits,
)
from course_supporter.normalizer.ports import EntryClassifier
from course_supporter.security.archive import (
    ClassifiedEntry,
    EntryVerdict,
    extract_archive_safely,
)
from course_supporter.storage.content_hash import compute_raw_hash

_MANIFEST_SCHEMA: Final[int] = 1
# Head slice handed to the classifier (matches the security layer's
# magic-detection window); the default classifier ignores it.
_HEAD_BYTES: Final[int] = 4096

_DEFAULT_LIMITS: Final[NormalizerLimits] = NormalizerLimits()
_DEFAULT_CLASSIFIER: Final[EntryClassifier] = ExtensionClassifier()


def normalize_archive(
    raw: bytes,
    *,
    archive_kind: Literal["zip", "tar.gz"],
    limits: NormalizerLimits = _DEFAULT_LIMITS,
    classifier: EntryClassifier = _DEFAULT_CLASSIFIER,
) -> NormalizedSnapshot:
    """Normalize a project archive into a snapshot + manifest + hash.

    Pipeline: sandbox-unpack (classify mode, level-1 unpack-guard from
    ``limits``) -> canonicalize paths -> collapse denylist directories ->
    map each survivor verdict (INCLUDED / FORBIDDEN_TYPE -> kept manifest
    entry via ``classifier``; MAGIC_MISMATCH / NESTED_ARCHIVE -> excluded
    row) -> enforce the level-2 content cap -> aggregate hash + canonical
    zip.

    Args:
        raw: Raw archive bytes (contract: a real project archive; the
            top-level is never a bare document -- see module docstring).
        archive_kind: ``"zip"`` or ``"tar.gz"``, resolved by the caller
            from the upload filename extension. Not autodetected.
        limits: Two-level limits. Level 1 (``raw_max_unzipped_bytes`` /
            ``raw_max_nesting_depth``) guards the raw unpack; level 2
            (``kept_total_max_bytes``) caps the cleaned snapshot.
            ``kept_single_max_bytes`` is a downstream (P4) threshold and
            is NOT applied here.
        classifier: Strategy mapping a kept entry to an
            :class:`~course_supporter.normalizer.models.EntryClass`.

    Returns:
        A :class:`NormalizedSnapshot` whose ``snapshot_hash`` equals the
        manifest ``aggregate_hash`` (the echo-match key).

    Raises:
        NormalizerLimitError: the cleaned snapshot exceeds
            ``limits.kept_total_max_bytes``.
        NormalizerError: two archive entries collapse to the same
            canonical path (degenerate / adversarial input).
        SecurityRejectedError: a structural archive violation from the
            security core -- path traversal, zip-bomb, symlink,
            malformed bytes, or a ``raw`` that does not parse as the
            declared ``archive_kind`` (mislabeled kind). Surfaced as-is
            with its ``ErrorCategory``, not translated.
    """
    classified: list[ClassifiedEntry] = list(
        extract_archive_safely(
            raw,
            archive_kind=archive_kind,
            max_unzipped_size=limits.raw_max_unzipped_bytes,
            max_nesting_depth=limits.raw_max_nesting_depth,
            allowed_extensions=KNOWN_EXTENSIONS,
            classify=True,
        )
    )

    # Split denylist-directory entries (collapsed to one row per prefix)
    # from survivors (mapped individually by verdict below).
    denied: list[tuple[str, int]] = []
    survivors: list[tuple[str, ClassifiedEntry]] = []
    for entry in classified:
        path = canonicalize_path(entry.arcname)
        if denylist_prefix(path) is not None:
            denied.append((path, len(entry.content)))
        else:
            survivors.append((path, entry))
    excluded_denylist = collapse_denylist(denied)

    seen: set[str] = set()
    included_pairs: list[tuple[ManifestEntry, bytes]] = []
    excluded_other: list[ExcludedEntry] = []
    for path, entry in survivors:
        verdict = entry.verdict
        if verdict is EntryVerdict.INCLUDED or verdict is EntryVerdict.FORBIDDEN_TYPE:
            # Both are kept: INCLUDED -> text/document by the classifier;
            # FORBIDDEN_TYPE (ext outside KNOWN_EXTENSIONS) -> binary.
            if path in seen:
                raise NormalizerError(
                    f"duplicate canonical path {path!r}: two archive "
                    "entries collapse to the same normalized path"
                )
            seen.add(path)
            cls = classifier.classify(path, entry.content[:_HEAD_BYTES])
            included_pairs.append(
                (
                    ManifestEntry(
                        path=path,
                        size=len(entry.content),
                        hash=compute_raw_hash(entry.content),
                        cls=cls,
                    ),
                    entry.content,
                )
            )
        elif verdict is EntryVerdict.MAGIC_MISMATCH:
            excluded_other.append(
                ExcludedEntry(
                    path=path,
                    reason=ExcludedReason.MAGIC_MISMATCH,
                    entries=1,
                    size=len(entry.content),
                )
            )
        elif verdict is EntryVerdict.NESTED_ARCHIVE:
            excluded_other.append(
                ExcludedEntry(
                    path=path,
                    reason=ExcludedReason.NESTED_ARCHIVE,
                    entries=1,
                    size=len(entry.content),
                )
            )
        else:
            assert_never(verdict)

    kept_total = sum(pair[0].size for pair in included_pairs)
    if kept_total > limits.kept_total_max_bytes:
        raise NormalizerLimitError(
            f"cleaned snapshot {kept_total} bytes exceeds level-2 cap "
            f"{limits.kept_total_max_bytes} bytes"
        )

    included_pairs.sort(key=lambda pair: pair[0].path)
    included = tuple(entry for entry, _ in included_pairs)
    excluded = tuple(
        sorted([*excluded_denylist, *excluded_other], key=lambda e: e.path)
    )
    aggregate = compute_aggregate_hash(included)
    canonical_zip = write_canonical_zip(
        [(entry.path, content) for entry, content in included_pairs]
    )

    manifest = Manifest(
        schema=_MANIFEST_SCHEMA,
        aggregate_hash=aggregate,
        included=included,
        excluded=excluded,
        total_files=len(included) + sum(e.entries for e in excluded),
        total_bytes=sum(e.size for e in included) + sum(e.size for e in excluded),
    )
    return NormalizedSnapshot(
        manifest=manifest,
        canonical_zip=canonical_zip,
        snapshot_hash=aggregate,
    )
