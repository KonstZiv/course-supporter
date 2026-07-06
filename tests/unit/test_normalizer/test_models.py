"""Tests for the normalizer data forms (KD18 P1) -- shapes only, no logic.

Proves the frozen/slots invariants (no mutation, no ``__dict__``) and
that the two enums are exhaustive at exactly the ratified member sets.
"""

import dataclasses

import pytest

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

_MANIFEST = Manifest(
    schema=1,
    aggregate_hash="h",
    included=(),
    excluded=(),
    total_files=0,
    total_bytes=0,
)


class TestEnumsExhaustive:
    def test_entry_class_members(self) -> None:
        assert {c.value for c in EntryClass} == {"text", "document", "binary"}

    def test_excluded_reason_members(self) -> None:
        assert {r.value for r in ExcludedReason} == {
            "denylist_dir",
            "magic_mismatch",
            "nested_archive",
        }

    def test_strenum_str_identity(self) -> None:
        # StrEnum members ARE their string value (JSONB / f-string safe).
        assert EntryClass.TEXT == "text"
        assert ExcludedReason.NESTED_ARCHIVE == "nested_archive"


class TestNormalizerLimitsDefaults:
    def test_ratified_numbers(self) -> None:
        limits = NormalizerLimits()
        assert limits.raw_max_unzipped_bytes == 150 * 1024 * 1024
        assert limits.raw_max_nesting_depth == 1
        assert limits.kept_total_max_bytes == 50 * 1024 * 1024
        assert limits.kept_single_max_bytes == 2 * 1024 * 1024


_FROZEN_SLOTS_CASES: list[tuple[type, dict[str, object]]] = [
    (
        ManifestEntry,
        {"path": "a.py", "size": 1, "hash": "h", "cls": EntryClass.TEXT},
    ),
    (
        ExcludedEntry,
        {
            "path": ".venv/",
            "reason": ExcludedReason.DENYLIST_DIR,
            "entries": 3,
            "size": 9,
        },
    ),
    (NormalizerLimits, {}),
    (
        Manifest,
        {
            "schema": 1,
            "aggregate_hash": "h",
            "included": (),
            "excluded": (),
            "total_files": 0,
            "total_bytes": 0,
        },
    ),
    (
        NormalizedSnapshot,
        {"manifest": _MANIFEST, "canonical_zip": b"", "snapshot_hash": "h"},
    ),
    (
        Delta,
        {"changed": (), "new": (), "deleted": (), "hygiene_new_excluded": ()},
    ),
]


class TestFrozenSlots:
    @pytest.mark.parametrize(("cls", "kwargs"), _FROZEN_SLOTS_CASES)
    def test_frozen_rejects_mutation(
        self, cls: type, kwargs: dict[str, object]
    ) -> None:
        inst = cls(**kwargs)
        field = next(iter(dataclasses.fields(inst))).name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(inst, field, "mutated")

    @pytest.mark.parametrize(("cls", "kwargs"), _FROZEN_SLOTS_CASES)
    def test_slots_has_no_dict(self, cls: type, kwargs: dict[str, object]) -> None:
        inst = cls(**kwargs)
        assert not hasattr(inst, "__dict__")
