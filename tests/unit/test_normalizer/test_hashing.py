"""Tests for the normalizer identity primitives (KD18 P1 byte-spec).

Locks the ``aggregate_hash`` byte-spec that the writer and the P3
matcher both depend on: canonicalization (leading ``./`` stripped),
order-invariance, and -- the byte the two sides can silently diverge on
-- no trailing newline in the joined blob.
"""

import hashlib

from course_supporter.normalizer.hashing import (
    canonicalize_path,
    compute_aggregate_hash,
)
from course_supporter.normalizer.models import EntryClass, ManifestEntry


def _entry(path: str, digest: str) -> ManifestEntry:
    return ManifestEntry(path=path, size=len(digest), hash=digest, cls=EntryClass.TEXT)


class TestCanonicalizePath:
    def test_strips_leading_dot_slash(self) -> None:
        assert canonicalize_path("./src/main.py") == "src/main.py"

    def test_collapses_dot_and_double_slash(self) -> None:
        assert canonicalize_path("src/./pkg//mod.py") == "src/pkg/mod.py"

    def test_plain_path_unchanged(self) -> None:
        assert canonicalize_path("src/main.py") == "src/main.py"


class TestAggregateHashDeterminism:
    def test_order_invariant(self) -> None:
        a = _entry("a.py", "h1")
        b = _entry("b.py", "h2")
        c = _entry("c.py", "h3")
        assert compute_aggregate_hash([a, b, c]) == compute_aggregate_hash([c, a, b])

    def test_leading_dot_slash_variant_same_hash(self) -> None:
        raw = _entry(canonicalize_path("./src/x.py"), "h")
        plain = _entry(canonicalize_path("src/x.py"), "h")
        assert compute_aggregate_hash([raw]) == compute_aggregate_hash([plain])

    def test_byte_exact_no_trailing_newline(self) -> None:
        e1 = _entry("a.py", "h1")
        e2 = _entry("b.py", "h2")
        # sorted -> ["a.py:h1", "b.py:h2"]; LF-joined; no trailing newline.
        expected = hashlib.sha256(b"a.py:h1\nb.py:h2").hexdigest()
        assert compute_aggregate_hash([e2, e1]) == expected

    def test_single_entry_has_no_trailing_newline(self) -> None:
        entry = _entry("only.py", "hh")
        expected = hashlib.sha256(b"only.py:hh").hexdigest()
        assert compute_aggregate_hash([entry]) == expected

    def test_empty_included_hashes_empty_blob(self) -> None:
        assert compute_aggregate_hash([]) == hashlib.sha256(b"").hexdigest()
