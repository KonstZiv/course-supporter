"""Migration literal ↔ formula-derived constant parity (Phase 3.1 Q-F).

The Alembic migration ``phase31_content_hash_defaults`` hardcodes
each empty-hash as a string literal per
[[feedback-migration-self-contained-no-app-import]] — Alembic must
not import application code. Q-F at the 3.1 pre-flight added a
counter-requirement: the literals MUST equal the constants derived
through the live ``compute_content_hash`` formula in
``storage/content_hash.py``. This test pins that parity, so any
future drift (e.g. someone tweaks ``_empty_node_summary_raw_payload``
without updating the migration literal) surfaces as a hard test
failure rather than silent regression at INSERT time.

Sibling-mirror to the migration literal — keep both files in sync.
"""

from __future__ import annotations

from course_supporter.storage.content_hash import (
    EMPTY_NODE_CONTENT_HASH,
    EMPTY_NODE_SUMMARY_FINAL_CONTENT_HASH,
    EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH,
)

# The same string literals as in
# ``migrations/versions/phase31_content_hash_defaults.py``. Pinned
# verbatim — divergence is the whole point of this test.
_MIGRATION_LITERAL_NODE_HASH = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
_MIGRATION_LITERAL_RAW_HASH = (
    "bcaf467defc6a11d9f437368b2388f310b8538fc2fe21621587f69175766cadd"
)
_MIGRATION_LITERAL_FINAL_HASH = (
    "85ef8d5f3881ffae484d1c1b47d18c0984b19c03bc9dd44e6fc1010e9fcdbf03"
)


def test_node_content_hash_literal_matches_constant() -> None:
    assert EMPTY_NODE_CONTENT_HASH == _MIGRATION_LITERAL_NODE_HASH


def test_node_summary_raw_content_hash_literal_matches_constant() -> None:
    assert EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH == _MIGRATION_LITERAL_RAW_HASH


def test_node_summary_final_content_hash_literal_matches_constant() -> None:
    assert EMPTY_NODE_SUMMARY_FINAL_CONTENT_HASH == _MIGRATION_LITERAL_FINAL_HASH


def test_three_constants_are_distinct() -> None:
    # Different payload shapes (CourseNode b"" vs Raw payload vs Final
    # payload) must produce different empty-hashes. A collision would
    # mean someone accidentally aligned the payload shapes — would
    # break memoization-by-empty-hash semantics in Phase 3.2.
    assert EMPTY_NODE_CONTENT_HASH != EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH
    assert EMPTY_NODE_CONTENT_HASH != EMPTY_NODE_SUMMARY_FINAL_CONTENT_HASH
    assert EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH != EMPTY_NODE_SUMMARY_FINAL_CONTENT_HASH
