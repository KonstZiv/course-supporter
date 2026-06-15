"""Unit tests for ``compute_node_summary_state`` (Task 3.2.5b commit-1).

DB-free derivation of the tree-feed summary badge. Covers the three
``summary_status`` values and the axis-1 ``materials_changed`` matrix,
plus the two correctness guards ratified for this commit:

* ``materials_changed`` is ``False`` whenever there is no Raw — never a
  leaked SQL ``NULL != ...`` (explicit guard in the pure function).
* ``materials_changed`` is INDEPENDENT of approval — an approved node
  whose materials changed stays ``True`` (the "approved but stale"
  signal); the two fields are orthogonal.
"""

from __future__ import annotations

import pytest

from course_supporter.storage.course_node_repository import (
    compute_node_summary_state,
)


def test_no_raw_is_none_and_never_materials_changed() -> None:
    """No active Raw → ``none`` status, ``materials_changed`` forced False.

    The source/node hashes are intentionally mismatched to prove the
    guard does not leak a comparison when there is nothing to compare.
    """
    status, materials_changed = compute_node_summary_state(
        raw_exists=False,
        final_approved=False,
        source_content_hash=None,
        node_content_hash="abc123",
    )
    assert status == "none"
    assert materials_changed is False


def test_raw_unapproved_is_draft() -> None:
    """Raw present, Final not approved → ``draft``."""
    status, materials_changed = compute_node_summary_state(
        raw_exists=True,
        final_approved=False,
        source_content_hash="hash-x",
        node_content_hash="hash-x",
    )
    assert status == "draft"
    assert materials_changed is False


def test_raw_approved_is_approved() -> None:
    """Raw present, Final approved → ``approved``."""
    status, materials_changed = compute_node_summary_state(
        raw_exists=True,
        final_approved=True,
        source_content_hash="hash-x",
        node_content_hash="hash-x",
    )
    assert status == "approved"
    assert materials_changed is False


def test_materials_changed_when_hashes_differ() -> None:
    """Draft node whose source hash drifted from the node hash → True."""
    status, materials_changed = compute_node_summary_state(
        raw_exists=True,
        final_approved=False,
        source_content_hash="generated-at-hash",
        node_content_hash="current-materials-hash",
    )
    assert status == "draft"
    assert materials_changed is True


def test_materials_changed_independent_of_approval() -> None:
    """Approved node with drifted materials stays ``materials_changed=True``.

    Approval must NOT mask the staleness signal — this is the useful
    "approved but stale, consider regenerating" case (Ratified #8).
    """
    status, materials_changed = compute_node_summary_state(
        raw_exists=True,
        final_approved=True,
        source_content_hash="generated-at-hash",
        node_content_hash="current-materials-hash",
    )
    assert status == "approved"
    assert materials_changed is True


def test_null_source_hash_with_raw_reads_as_changed() -> None:
    """Raw exists but ``source_content_hash`` is NULL (pre-Pass-1 cache key).

    Mirrors ``_is_axis1_fresh`` in the orchestrator: a missing cache key
    is treated as stale (``None != node_hash`` → changed), consistent
    with the generate-route's axis-1 semantics.
    """
    status, materials_changed = compute_node_summary_state(
        raw_exists=True,
        final_approved=False,
        source_content_hash=None,
        node_content_hash="current-materials-hash",
    )
    assert status == "draft"
    assert materials_changed is True


@pytest.mark.parametrize(
    ("final_approved", "expected_status"),
    [(False, "draft"), (True, "approved")],
)
def test_status_matrix(final_approved: bool, expected_status: str) -> None:
    """Status tracks approval when a Raw exists."""
    status, _ = compute_node_summary_state(
        raw_exists=True,
        final_approved=final_approved,
        source_content_hash="h",
        node_content_hash="h",
    )
    assert status == expected_status
