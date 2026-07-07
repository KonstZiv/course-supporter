"""Manifest ↔ JSONB serde for the deterministic normalizer (KD18 P3).

The write side (:func:`manifest_to_jsonb`) was introduced in P2's base-normalize
worker; P3 adds the read side (:func:`manifest_from_jsonb`) — needed to
reconstruct a persisted base / submission ``Manifest`` for
:func:`course_supporter.normalizer.compute_delta`. Both sides live in one module
(write + read adjacent) so the JSONB shape has a single source of truth and the
two directions cannot drift.

Stored shape — ``dataclasses.asdict`` of the frozen ``Manifest`` with StrEnum
members unwrapped to their values::

    {"schema": 1, "aggregate_hash": "...",
     "included": [{"path": ..., "size": ..., "hash": ..., "cls": "text"}],
     "excluded": [{"path": ..., "reason": "denylist_dir",
                   "entries": ..., "size": ...}],
     "total_files": ..., "total_bytes": ...}
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum
from typing import Any

from course_supporter.normalizer.models import (
    EntryClass,
    ExcludedEntry,
    ExcludedReason,
    Manifest,
    ManifestEntry,
)


def manifest_to_jsonb(manifest: Manifest) -> dict[str, Any]:
    """Serialize a frozen ``Manifest`` to a JSONB-ready dict (StrEnum → value).

    ``dataclasses.asdict`` with a factory that unwraps ``StrEnum`` members
    (``EntryClass`` / ``ExcludedReason``) to their string value. The stored
    shape mirrors the dataclass field names (``cls`` / ``total_files`` /
    ``total_bytes``), tagged by ``schema`` for forward evolution.
    """

    def _factory(items: list[tuple[str, Any]]) -> dict[str, Any]:
        return {k: (v.value if isinstance(v, StrEnum) else v) for k, v in items}

    return dataclasses.asdict(manifest, dict_factory=_factory)


def manifest_from_jsonb(payload: dict[str, Any]) -> Manifest:
    """Reconstruct a frozen ``Manifest`` from its JSONB dict.

    The exact inverse of :func:`manifest_to_jsonb`: revives the ``StrEnum``
    members (``cls`` → :class:`EntryClass`, ``reason`` → :class:`ExcludedReason`)
    and rebuilds the frozen tuples, so
    ``manifest_from_jsonb(manifest_to_jsonb(m)) == m``.

    P3 uses it to load a persisted base ``manifest`` (from ``project_bases``) and
    a submission ``snapshot_manifest`` back into ``Manifest`` objects for
    :func:`course_supporter.normalizer.compute_delta`.
    """
    included = tuple(
        ManifestEntry(
            path=entry["path"],
            size=entry["size"],
            hash=entry["hash"],
            cls=EntryClass(entry["cls"]),
        )
        for entry in payload["included"]
    )
    excluded = tuple(
        ExcludedEntry(
            path=entry["path"],
            reason=ExcludedReason(entry["reason"]),
            entries=entry["entries"],
            size=entry["size"],
        )
        for entry in payload["excluded"]
    )
    return Manifest(
        schema=payload["schema"],
        aggregate_hash=payload["aggregate_hash"],
        included=included,
        excluded=excluded,
        total_files=payload["total_files"],
        total_bytes=payload["total_bytes"],
    )
