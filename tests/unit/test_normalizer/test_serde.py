"""Tests for the Manifest ↔ JSONB serde (KD18 P3).

Locks the round-trip contract shared by the write side (``manifest_to_jsonb``,
introduced in P2's base-normalize worker) and the read side
(``manifest_from_jsonb``, added in P3 to reconstruct a persisted manifest for
``compute_delta``). If these two drift, echo-match / delta break silently.
"""

from typing import Any

from course_supporter.normalizer.models import (
    EntryClass,
    ExcludedEntry,
    ExcludedReason,
    Manifest,
    ManifestEntry,
)
from course_supporter.normalizer.serde import (
    manifest_from_jsonb,
    manifest_to_jsonb,
)


def _rich_manifest() -> Manifest:
    """A manifest exercising every EntryClass + ExcludedReason member."""
    included = (
        ManifestEntry(path="src/main.py", size=42, hash="a" * 64, cls=EntryClass.TEXT),
        ManifestEntry(
            path="docs/spec.pdf", size=99, hash="b" * 64, cls=EntryClass.DOCUMENT
        ),
        ManifestEntry(
            path="assets/logo.png", size=7, hash="c" * 64, cls=EntryClass.BINARY
        ),
    )
    excluded = (
        ExcludedEntry(
            path=".venv/", reason=ExcludedReason.DENYLIST_DIR, entries=812, size=0
        ),
        ExcludedEntry(
            path="fake.py", reason=ExcludedReason.MAGIC_MISMATCH, entries=1, size=3
        ),
        ExcludedEntry(
            path="inner.zip", reason=ExcludedReason.NESTED_ARCHIVE, entries=1, size=5
        ),
    )
    return Manifest(
        schema=1,
        aggregate_hash="d" * 64,
        included=included,
        excluded=excluded,
        total_files=6,
        total_bytes=48,
    )


class TestRoundTrip:
    def test_from_of_to_is_identity(self) -> None:
        """manifest_from_jsonb(manifest_to_jsonb(m)) == m (frozen equality)."""
        m = _rich_manifest()
        assert manifest_from_jsonb(manifest_to_jsonb(m)) == m

    def test_empty_manifest_round_trips(self) -> None:
        m = Manifest(
            schema=1,
            aggregate_hash="e" * 64,
            included=(),
            excluded=(),
            total_files=0,
            total_bytes=0,
        )
        assert manifest_from_jsonb(manifest_to_jsonb(m)) == m


class TestWriteShape:
    def test_strenum_unwrapped_to_value(self) -> None:
        """The stored dict carries plain string values, not StrEnum members."""
        payload = manifest_to_jsonb(_rich_manifest())
        assert payload["included"][0]["cls"] == "text"
        assert payload["included"][1]["cls"] == "document"
        assert payload["excluded"][0]["reason"] == "denylist_dir"
        assert payload["excluded"][2]["reason"] == "nested_archive"
        # asdict field names (the P2-canonized shape), not the chern draft.
        assert set(payload) == {
            "schema",
            "aggregate_hash",
            "included",
            "excluded",
            "total_files",
            "total_bytes",
        }


class TestReviveTypes:
    def test_revives_strenum_members(self) -> None:
        """cls / reason come back as StrEnum members, not bare strings."""
        payload: dict[str, Any] = manifest_to_jsonb(_rich_manifest())
        revived = manifest_from_jsonb(payload)
        assert isinstance(revived.included[0].cls, EntryClass)
        assert revived.included[0].cls is EntryClass.TEXT
        assert isinstance(revived.excluded[0].reason, ExcludedReason)
        assert revived.excluded[0].reason is ExcludedReason.DENYLIST_DIR

    def test_tuples_are_frozen(self) -> None:
        revived = manifest_from_jsonb(manifest_to_jsonb(_rich_manifest()))
        assert isinstance(revived.included, tuple)
        assert isinstance(revived.excluded, tuple)
