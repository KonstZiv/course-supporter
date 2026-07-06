"""Deterministic normalizer for project-archive base + delta (KD18 P1).

Domain-neutral library: sandbox-unpack (via the security layer's
``extract_archive_safely`` classify mode) -> denylist / classify ->
canonical snapshot + algorithmic manifest + aggregate hash, plus the
``base``-vs-``submission`` delta. Zero LLM, zero new dependencies. This
package exposes only forms + identity primitives + ports for now; the
extraction and orchestration logic land in later commits.
"""

from course_supporter.normalizer.canonical_zip import write_canonical_zip
from course_supporter.normalizer.classify import (
    KNOWN_EXTENSIONS,
    ExtensionClassifier,
    collapse_denylist,
    denylist_prefix,
)
from course_supporter.normalizer.hashing import (
    canonicalize_path,
    compute_aggregate_hash,
)
from course_supporter.normalizer.models import (
    Delta,
    EntryClass,
    ExcludedEntry,
    ExcludedReason,
    Manifest,
    ManifestEntry,
    NormalizedSnapshot,
    NormalizerLimits,
)
from course_supporter.normalizer.ports import EntryClassifier, TextExtractor

__all__ = [
    "KNOWN_EXTENSIONS",
    "Delta",
    "EntryClass",
    "EntryClassifier",
    "ExcludedEntry",
    "ExcludedReason",
    "ExtensionClassifier",
    "Manifest",
    "ManifestEntry",
    "NormalizedSnapshot",
    "NormalizerLimits",
    "TextExtractor",
    "canonicalize_path",
    "collapse_denylist",
    "compute_aggregate_hash",
    "denylist_prefix",
    "write_canonical_zip",
]
