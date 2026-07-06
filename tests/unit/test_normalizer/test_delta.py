"""Tests for compute_delta (KD18 P1) -- path-level base-vs-submission diff."""

from course_supporter.normalizer.delta import compute_delta
from course_supporter.normalizer.models import (
    EntryClass,
    ExcludedEntry,
    ExcludedReason,
    Manifest,
    ManifestEntry,
)


def _me(path: str, digest: str) -> ManifestEntry:
    return ManifestEntry(path=path, size=len(digest), hash=digest, cls=EntryClass.TEXT)


def _ex(path: str) -> ExcludedEntry:
    return ExcludedEntry(
        path=path, reason=ExcludedReason.DENYLIST_DIR, entries=1, size=0
    )


def _manifest(
    included: list[tuple[str, str]],
    excluded: tuple[str, ...] = (),
) -> Manifest:
    inc = tuple(_me(p, h) for p, h in included)
    exc = tuple(_ex(p) for p in excluded)
    return Manifest(
        schema=1,
        aggregate_hash="",
        included=inc,
        excluded=exc,
        total_files=len(inc) + len(exc),
        total_bytes=0,
    )


class TestBasicSets:
    def test_identical_is_empty_delta(self) -> None:
        m = _manifest([("a.py", "h1"), ("b.py", "h2")])
        d = compute_delta(m, m)
        assert d.changed == ()
        assert d.new == ()
        assert d.deleted == ()
        assert d.hygiene_new_excluded == ()

    def test_changed_is_hash_inequality(self) -> None:
        base = _manifest([("main.py", "h1")])
        sub = _manifest([("main.py", "h2")])
        d = compute_delta(base, sub)
        assert d.changed == ("main.py",)
        assert d.new == () and d.deleted == ()

    def test_same_hash_not_changed(self) -> None:
        base = _manifest([("main.py", "h1")])
        sub = _manifest([("main.py", "h1")])
        assert compute_delta(base, sub).changed == ()

    def test_new(self) -> None:
        base = _manifest([("a.py", "h")])
        sub = _manifest([("a.py", "h"), ("b.py", "h2")])
        assert compute_delta(base, sub).new == ("b.py",)

    def test_deleted(self) -> None:
        base = _manifest([("a.py", "h"), ("b.py", "h2")])
        sub = _manifest([("a.py", "h")])
        assert compute_delta(base, sub).deleted == ("b.py",)

    def test_mixed(self) -> None:
        base = _manifest([("a.py", "h1"), ("b.py", "h2"), ("c.py", "h3")])
        sub = _manifest([("a.py", "h1"), ("b.py", "hX"), ("d.py", "h4")])
        d = compute_delta(base, sub)
        assert d.changed == ("b.py",)
        assert d.new == ("d.py",)
        assert d.deleted == ("c.py",)


class TestHygiene:
    def test_new_excluded_surfaced(self) -> None:
        base = _manifest([("main.py", "h")])
        sub = _manifest([("main.py", "h")], excluded=(".venv/",))
        assert compute_delta(base, sub).hygiene_new_excluded == (".venv/",)

    def test_excluded_present_in_base_not_flagged(self) -> None:
        base = _manifest([("main.py", "h")], excluded=(".venv/",))
        sub = _manifest([("main.py", "h")], excluded=(".venv/",))
        assert compute_delta(base, sub).hygiene_new_excluded == ()

    def test_duplicate_excluded_paths_deduped(self) -> None:
        # Two excluded rows share a path (e.g. two magic-mismatches) ->
        # one hygiene entry.
        base = _manifest([("main.py", "h")])
        sub = _manifest([("main.py", "h")], excluded=("dup.py", "dup.py"))
        assert compute_delta(base, sub).hygiene_new_excluded == ("dup.py",)


class TestEdges:
    def test_empty_base_all_new(self) -> None:
        base = _manifest([])
        sub = _manifest([("a.py", "h"), ("b.py", "h2")])
        d = compute_delta(base, sub)
        assert d.new == ("a.py", "b.py")
        assert d.changed == () and d.deleted == ()

    def test_path_in_both_deleted_and_hygiene(self) -> None:
        # A file that moved INCLUDED -> excluded (e.g. a .py that flipped
        # to magic-mismatch): deleted from the review set AND a new
        # excluded entry. Two orthogonal signals, not deduplicated.
        base = _manifest([("main.py", "h")])
        sub = _manifest([], excluded=("main.py",))
        d = compute_delta(base, sub)
        assert d.deleted == ("main.py",)
        assert d.hygiene_new_excluded == ("main.py",)

    def test_outputs_sorted(self) -> None:
        base = _manifest([])
        sub = _manifest([("z.py", "h"), ("a.py", "h2"), ("m.py", "h3")])
        assert compute_delta(base, sub).new == ("a.py", "m.py", "z.py")
