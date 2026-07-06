"""Canonical identity primitives for the normalizer (KD18 P1).

Two pure functions are the single source of truth for a snapshot's
identity, so the normalizer that writes ``aggregate_hash`` and the P3
matcher that verifies it can never diverge on the byte-spec. No I/O, no
extraction -- this is the executable form of the :class:`Manifest`
byte-spec, kept next to (not duplicated across) its callers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import PurePosixPath

from course_supporter.normalizer.models import ManifestEntry


def canonicalize_path(arcname: str) -> str:
    """Return the canonical POSIX path for a validated archive name.

    ``str(PurePosixPath(arcname))`` strips a leading ``./``, collapses
    ``.`` and ``//`` segments, and keeps forward-slash separators. The
    security layer already rejected ``..`` / backslash / absolute paths
    and applied NFKC, so this is the one remaining canonicalization that
    makes the aggregate hash stable across repackaging (``./src/x`` and
    ``src/x`` converge here).
    """
    return str(PurePosixPath(arcname))


def compute_aggregate_hash(included: Sequence[ManifestEntry]) -> str:
    """SHA-256 hex of the snapshot's included entries (Manifest byte-spec).

    ``sorted(f"{path}:{hash}")`` joined by LF with no trailing newline,
    UTF-8 encoded. Excluded entries do not participate. See the
    :class:`~course_supporter.normalizer.models.Manifest` docstring for
    the authoritative specification.
    """
    lines = sorted(f"{entry.path}:{entry.hash}" for entry in included)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
