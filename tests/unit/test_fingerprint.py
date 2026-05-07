"""Unit tests for FingerprintService — MagicMock-based, fast.

Tests current (half-aligned) FingerprintService behavior. Service is
scheduled for collapse into ``ContentHashService`` per Phase 1.1 plan
+ vision.md §3 KD9 line 572 acknowledged regression. Tests serve as
regression guard during Phase 1.1 transition; they will be migrated
to ``ContentHashService`` coverage when collapse lands.

Does NOT verify vision §3 KD9 alignment (``raw_hash`` at
AuthoredDocument level + DocumentSummary inclusion in CourseNode hash
composition) — those invariants belong to ``ContentHashService`` and
will be tested in its own test suite.
"""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.fingerprint import FingerprintService
from course_supporter.storage.orm import AuthoredDocument, CourseNode


def _hash(content: str | bytes) -> str:
    """Helper: SHA-256 hex of a string or bytes input."""
    data = content.encode() if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def _make_entry(*, content_hash: str | None = None) -> MagicMock:
    """Create a mock AuthoredDocument with content_hash set."""
    entry = MagicMock(spec=AuthoredDocument)
    entry.id = uuid.uuid4()
    entry.content_hash = content_hash
    return entry


def _make_node(
    *,
    documents: list[MagicMock] | None = None,
    children: list[MagicMock] | None = None,
    content_hash: str | None = None,
) -> MagicMock:
    """Create a mock CourseNode with documents and children."""
    node = MagicMock(spec=CourseNode)
    node.id = uuid.uuid4()
    node.documents = documents or []
    node.children = children or []
    node.content_hash = content_hash
    return node


class TestEnsureMaterialFp:
    async def test_returns_processed_hash(self) -> None:
        """ensure_material_fp returns the entry's content_hash."""
        content = "Hello, this is processed content."
        entry = _make_entry(content_hash=_hash(content))
        session = AsyncMock()

        svc = FingerprintService(session)
        result = await svc.ensure_material_fp(entry)

        assert result == _hash(content)
        assert result == entry.content_hash

    async def test_raises_when_no_processed_hash(self) -> None:
        """ValueError if content_hash is None."""
        entry = _make_entry()
        session = AsyncMock()
        svc = FingerprintService(session)

        with pytest.raises(ValueError, match="no content_hash"):
            await svc.ensure_material_fp(entry)

    async def test_deterministic_same_content_same_hash(self) -> None:
        """Same content_hash always produces the same fingerprint."""
        content = "deterministic test"
        session = AsyncMock()
        svc = FingerprintService(session)

        entry1 = _make_entry(content_hash=_hash(content))
        entry2 = _make_entry(content_hash=_hash(content))

        fp1 = await svc.ensure_material_fp(entry1)
        fp2 = await svc.ensure_material_fp(entry2)

        assert fp1 == fp2

    async def test_different_content_different_hash(self) -> None:
        """Different content_hash produces different fingerprints."""
        session = AsyncMock()
        svc = FingerprintService(session)

        entry1 = _make_entry(content_hash=_hash("content A"))
        entry2 = _make_entry(content_hash=_hash("content B"))

        fp1 = await svc.ensure_material_fp(entry1)
        fp2 = await svc.ensure_material_fp(entry2)

        assert fp1 != fp2

    async def test_fingerprint_is_64_char_hex(self) -> None:
        """Fingerprint is a valid 64-character hex string (sha256)."""
        entry = _make_entry(content_hash=_hash("test"))
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_material_fp(entry)

        assert len(result) == 64
        int(result, 16)  # valid hex


class TestRepositoryInvalidation:
    """Verify that repository methods invalidate node fingerprint chain."""

    async def test_complete_processing_does_not_invalidate_node_chain(self) -> None:
        """complete_processing is intentionally state-transition-only.

        Per hotfix-9 (b9fadc8) + vision §1.2: complete_processing clears
        ``job_id`` / ``pending_since`` / ``error_message`` and sets
        ``processed_at``, but does NOT cascade invalidate parent hashes.
        Cascade invalidation belongs to ``update_source`` (raw bytes
        change) and ``create`` (initial insertion); state transitions
        alone do not affect content hash.

        This negative assertion serves as regression guard: if cascade
        invalidation is re-added to ``complete_processing`` without
        review, this test will catch it. Phase 2.x KD2 may revisit this
        if ``DocumentSegment`` / ``DocumentSummary`` persistence on
        AuthoredDocument level changes the contract.
        """
        from course_supporter.storage.authored_document_repository import (
            AuthoredDocumentRepository,
        )

        entry = MagicMock(spec=AuthoredDocument)

        session = AsyncMock()
        repo = AuthoredDocumentRepository(session)
        invalidate_mock = AsyncMock()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_require", AsyncMock(return_value=entry))
            mp.setattr(repo, "_invalidate_node_chain", invalidate_mock)
            await repo.complete_processing(entry.id)

        invalidate_mock.assert_not_awaited()

    async def test_update_source_invalidates_node_chain(self) -> None:
        """update_source triggers node chain invalidation."""
        from course_supporter.storage.authored_document_repository import (
            AuthoredDocumentRepository,
        )

        entry = MagicMock(spec=AuthoredDocument)

        session = AsyncMock()
        repo = AuthoredDocumentRepository(session)
        invalidate_mock = AsyncMock()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_require", AsyncMock(return_value=entry))
            mp.setattr(repo, "_invalidate_node_chain", invalidate_mock)
            await repo.update_source(
                entry.id,
                source_url="https://new-url.com",
            )

        invalidate_mock.assert_awaited_once_with(entry.course_node_id)


class TestEnsureNodeFp:
    """Tests for ensure_node_fp — Merkle hash of a node subtree."""

    async def test_empty_node(self) -> None:
        """Empty node (no materials, no children) returns deterministic hash."""
        node = _make_node()
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        expected = _hash(b"")
        assert result == expected
        assert node.content_hash == expected
        session.flush.assert_awaited()

    async def test_single_material(self) -> None:
        """Node with one processed material includes its fingerprint."""
        mat = _make_entry(content_hash=_hash("lesson text"))
        node = _make_node(documents=[mat])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        mat_fp = _hash("lesson text")
        expected = _hash(f"m:{mat_fp}")
        assert result == expected

    async def test_skips_unprocessed_materials(self) -> None:
        """Materials without content_hash are excluded."""
        processed = _make_entry(content_hash=_hash("done"))
        raw = _make_entry(content_hash=None)
        node = _make_node(documents=[processed, raw])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        # Same as node with only the processed material
        node_single = _make_node(documents=[_make_entry(content_hash=_hash("done"))])
        result_single = await svc.ensure_node_fp(node_single)
        assert result == result_single

    async def test_single_child_node(self) -> None:
        """Node with one child includes child's Merkle hash."""
        child = _make_node(documents=[_make_entry(content_hash=_hash("child text"))])
        parent = _make_node(children=[child])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(parent)

        # Child fp first
        child_mat_fp = _hash("child text")
        child_fp = _hash(f"m:{child_mat_fp}")
        expected = _hash(f"n:{child_fp}")
        assert result == expected

    async def test_nested_3_levels(self) -> None:
        """Merkle hash propagates correctly through 3 levels."""
        leaf = _make_node(documents=[_make_entry(content_hash=_hash("leaf"))])
        mid = _make_node(children=[leaf])
        root = _make_node(children=[mid])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(root)

        # Compute bottom-up
        leaf_mat_fp = _hash("leaf")
        leaf_fp = _hash(f"m:{leaf_mat_fp}")
        mid_fp = _hash(f"n:{leaf_fp}")
        expected = _hash(f"n:{mid_fp}")
        assert result == expected

    async def test_deterministic_same_data_same_hash(self) -> None:
        """Same tree structure + content produces the same hash."""
        session = AsyncMock()
        svc = FingerprintService(session)

        node1 = _make_node(documents=[_make_entry(content_hash=_hash("aaa"))])
        node2 = _make_node(documents=[_make_entry(content_hash=_hash("aaa"))])

        fp1 = await svc.ensure_node_fp(node1)
        fp2 = await svc.ensure_node_fp(node2)
        assert fp1 == fp2

    async def test_cache_hit_returns_existing(self) -> None:
        """If content_hash is already set, return it without recalc."""
        cached = "b" * 64
        node = _make_node(content_hash=cached)
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        assert result == cached
        session.flush.assert_not_awaited()

    async def test_invalidation_then_recalculate(self) -> None:
        """After clearing content_hash, next call recomputes."""
        node = _make_node(documents=[_make_entry(content_hash=_hash("data"))])
        session = AsyncMock()
        svc = FingerprintService(session)

        fp1 = await svc.ensure_node_fp(node)
        assert node.content_hash == fp1

        # Invalidate
        node.content_hash = None

        fp2 = await svc.ensure_node_fp(node)
        assert fp2 == fp1  # same data → same hash

    async def test_parts_are_sorted(self) -> None:
        """Material and child parts are sorted before hashing."""
        session = AsyncMock()
        svc = FingerprintService(session)

        mat_a = _make_entry(content_hash=_hash("aaa"))
        mat_b = _make_entry(content_hash=_hash("bbb"))

        # Order of materials should not affect fingerprint
        node1 = _make_node(documents=[mat_a, mat_b])
        node2 = _make_node(
            documents=[
                _make_entry(content_hash=_hash("bbb")),
                _make_entry(content_hash=_hash("aaa")),
            ],
        )

        fp1 = await svc.ensure_node_fp(node1)
        fp2 = await svc.ensure_node_fp(node2)
        assert fp1 == fp2

    async def test_materials_and_children_mixed(self) -> None:
        """Node with both materials and children combines all parts."""
        child = _make_node(documents=[_make_entry(content_hash=_hash("child"))])
        mat = _make_entry(content_hash=_hash("parent mat"))
        parent = _make_node(documents=[mat], children=[child])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(parent)

        # Compute expected
        mat_fp = _hash("parent mat")
        child_mat_fp = _hash("child")
        child_fp = _hash(f"m:{child_mat_fp}")
        parts = sorted([f"m:{mat_fp}", f"n:{child_fp}"])
        expected = _hash("\n".join(parts))
        assert result == expected


class TestEnsureCourseFp:
    """Tests for ensure_course_fp — course-level Merkle hash."""

    async def test_empty_course_no_roots(self) -> None:
        """Course with no root nodes returns hash of empty string."""
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_course_fp([])

        expected = _hash(b"")
        assert result == expected

    async def test_single_root(self) -> None:
        """Course with one root node returns hash of that root's fp."""
        root = _make_node(documents=[_make_entry(content_hash=_hash("data"))])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_course_fp([root])

        root_fp = _hash(f"m:{_hash('data')}")
        expected = _hash(root_fp)
        assert result == expected

    async def test_multiple_roots_sorted(self) -> None:
        """Root node order does not affect course fingerprint."""
        root_a = _make_node(documents=[_make_entry(content_hash=_hash("aaa"))])
        root_b = _make_node(documents=[_make_entry(content_hash=_hash("bbb"))])
        session = AsyncMock()
        svc = FingerprintService(session)

        fp1 = await svc.ensure_course_fp([root_a, root_b])

        # Reverse order
        root_a2 = _make_node(documents=[_make_entry(content_hash=_hash("aaa"))])
        root_b2 = _make_node(documents=[_make_entry(content_hash=_hash("bbb"))])
        fp2 = await svc.ensure_course_fp([root_b2, root_a2])

        assert fp1 == fp2

    async def test_stable_when_nothing_changes(self) -> None:
        """Same tree produces same course fingerprint."""
        session = AsyncMock()
        svc = FingerprintService(session)

        root1 = _make_node(documents=[_make_entry(content_hash=_hash("x"))])
        root2 = _make_node(documents=[_make_entry(content_hash=_hash("x"))])

        fp1 = await svc.ensure_course_fp([root1])
        fp2 = await svc.ensure_course_fp([root2])
        assert fp1 == fp2

    async def test_changes_when_material_changes(self) -> None:
        """Course fp changes when any material content changes."""
        session = AsyncMock()
        svc = FingerprintService(session)

        root_v1 = _make_node(documents=[_make_entry(content_hash=_hash("v1"))])
        root_v2 = _make_node(documents=[_make_entry(content_hash=_hash("v2"))])

        fp1 = await svc.ensure_course_fp([root_v1])
        fp2 = await svc.ensure_course_fp([root_v2])
        assert fp1 != fp2

    async def test_single_flush(self) -> None:
        """ensure_course_fp issues exactly one flush."""
        root = _make_node(
            children=[
                _make_node(documents=[_make_entry(content_hash=_hash("a"))]),
                _make_node(documents=[_make_entry(content_hash=_hash("b"))]),
            ],
        )
        session = AsyncMock()
        svc = FingerprintService(session)

        await svc.ensure_course_fp([root])

        session.flush.assert_awaited_once()


class TestInvalidateUp:
    """Tests for invalidate_up — cascade fingerprint invalidation."""

    async def test_leaf_to_root_all_invalidated(self) -> None:
        """All ancestors from leaf to root get content_hash=None."""
        leaf = _make_node(content_hash="leaf_fp")
        mid = _make_node(content_hash="mid_fp")
        root = _make_node(content_hash="root_fp")

        leaf.parent_id = mid.id
        mid.parent_id = root.id
        root.parent_id = None

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda _cls, pid: {mid.id: mid, root.id: root}.get(pid)
        )

        svc = FingerprintService(session)
        await svc.invalidate_up(leaf)

        assert leaf.content_hash is None
        assert mid.content_hash is None
        assert root.content_hash is None

    async def test_root_node_only(self) -> None:
        """Root node (no parent) gets invalidated, no further walk."""
        root = _make_node(content_hash="root_fp")
        root.parent_id = None

        session = AsyncMock()
        svc = FingerprintService(session)
        await svc.invalidate_up(root)

        assert root.content_hash is None
        session.get.assert_not_awaited()

    async def test_siblings_untouched(self) -> None:
        """Sibling nodes are not affected by invalidation."""
        leaf = _make_node(content_hash="leaf_fp")
        sibling = _make_node(content_hash="sibling_fp")
        parent = _make_node(content_hash="parent_fp")

        leaf.parent_id = parent.id
        sibling.parent_id = parent.id
        parent.parent_id = None

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda _cls, pid: {parent.id: parent}.get(pid)
        )

        svc = FingerprintService(session)
        await svc.invalidate_up(leaf)

        assert leaf.content_hash is None
        assert parent.content_hash is None
        assert sibling.content_hash == "sibling_fp"  # untouched

    async def test_single_flush_after_walk(self) -> None:
        """Only one flush after the entire chain walk."""
        leaf = _make_node(content_hash="fp")
        mid = _make_node(content_hash="fp")
        root = _make_node(content_hash="fp")

        leaf.parent_id = mid.id
        mid.parent_id = root.id
        root.parent_id = None

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda _cls, pid: {mid.id: mid, root.id: root}.get(pid)
        )

        svc = FingerprintService(session)
        await svc.invalidate_up(leaf)

        session.flush.assert_awaited_once()

    async def test_already_none_still_walks(self) -> None:
        """Even if a node has fingerprint=None, walk continues upward."""
        leaf = _make_node(content_hash="fp")
        mid = _make_node(content_hash=None)  # already invalidated
        root = _make_node(content_hash="root_fp")

        leaf.parent_id = mid.id
        mid.parent_id = root.id
        root.parent_id = None

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda _cls, pid: {mid.id: mid, root.id: root}.get(pid)
        )

        svc = FingerprintService(session)
        await svc.invalidate_up(leaf)

        assert root.content_hash is None  # still reached and cleared


class TestRepositoryCascadeInvalidation:
    """Tests for auto-invalidation in repository CRUD methods (S2-028)."""

    # ``test_entry_create_invalidates_node`` removed in Phase 1.1 etap
    # 1.1.4: ``AuthoredDocumentRepository.create()`` no longer routes
    # through ``_invalidate_node_chain`` — it now calls
    # ``ContentHashService.invalidate_up(entry)`` directly post-flush
    # (per INVESTIGATION §6.7.1 variant (a)). The create()-time
    # materialisation invariant is covered by
    # ``tests/integration/test_content_hash_persistence.py::TestCreate
    # MaterializesContentHash``. The whole file deletes wholesale in C3.

    async def test_entry_complete_processing_does_not_invalidate_node(self) -> None:
        """AuthoredDocumentRepository.complete_processing intentionally skips cascade.

        Symmetric regression guard to
        ``TestRepositoryInvalidation::test_complete_processing_does_not_invalidate_node_chain``
        — verifies state-transition method does not trigger cascade
        invalidation per hotfix-9 design.
        """
        from course_supporter.storage.authored_document_repository import (
            AuthoredDocumentRepository,
        )

        entry = MagicMock(spec=AuthoredDocument)
        entry.course_node_id = uuid.uuid4()

        session = AsyncMock()
        repo = AuthoredDocumentRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_require", AsyncMock(return_value=entry))
            mock_inv = AsyncMock()
            mp.setattr(repo, "_invalidate_node_chain", mock_inv)
            await repo.complete_processing(entry.id)

        mock_inv.assert_not_awaited()

    async def test_entry_update_source_invalidates_node(self) -> None:
        """AuthoredDocumentRepository.update_source triggers cascade."""
        from course_supporter.storage.authored_document_repository import (
            AuthoredDocumentRepository,
        )

        entry = MagicMock(spec=AuthoredDocument)
        entry.course_node_id = uuid.uuid4()

        session = AsyncMock()
        repo = AuthoredDocumentRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_require", AsyncMock(return_value=entry))
            mock_inv = AsyncMock()
            mp.setattr(repo, "_invalidate_node_chain", mock_inv)
            await repo.update_source(entry.id, source_url="https://new.com")

        mock_inv.assert_awaited_once_with(entry.course_node_id)

    async def test_node_move_invalidates_old_and_new_parent(self) -> None:
        """CourseNodeRepository.move invalidates both parent chains."""
        from course_supporter.storage.course_node_repository import (
            CourseNodeRepository,
        )

        old_parent_id = uuid.uuid4()
        new_parent_id = uuid.uuid4()
        node = MagicMock(spec=CourseNode)
        node.id = uuid.uuid4()
        node.parent_id = old_parent_id
        node.course_id = uuid.uuid4()

        session = AsyncMock()
        repo = CourseNodeRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_by_id", AsyncMock(return_value=node))
            mp.setattr(repo, "_is_descendant", AsyncMock(return_value=False))
            mp.setattr(repo, "_next_sibling_order", AsyncMock(return_value=0))
            mock_inv = AsyncMock()
            mp.setattr(repo, "_invalidate_node_chain", mock_inv)
            await repo.move(node.id, new_parent_id)

        assert mock_inv.await_count == 2
        mock_inv.assert_any_await(old_parent_id)
        mock_inv.assert_any_await(new_parent_id)

    # NOTE: ``CourseNodeRepository.delete()`` removed in Phase 1
    # commit (k) per KD3 adoption — tests
    # ``test_node_delete_invalidates_parent`` and
    # ``test_node_delete_root_skips_invalidation`` removed alongside.
    # The fingerprint-invalidation responsibility now lives in the
    # cascade engine's ``on_invalidate_hashes`` hook bound to
    # :meth:`ContentHashService.invalidate_subtree` (Gap 3 fix —
    # commit (i)). Coverage moves to
    # ``tests/storage/test_cascade_invalidation.py``.
    #
    # Phase 1 cleanup-chain Commit 7 (kd3-fix-12) additionally drops
    # ``test_entry_delete_invalidates_node`` for the same reason —
    # ``AuthoredDocumentRepository.delete()`` was removed per KD-alpha
    # soft-delete supersession; cascade now lives in
    # ``CascadeDeleteService``. Coverage in
    # ``tests/storage/test_cascade_kd_alpha.py``.


class TestKnownHash:
    """Verify Merkle hashes against pre-computed known values."""

    async def test_material_known_sha256(self) -> None:
        """Material fingerprint matches independently computed sha256."""
        known = "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
        entry = _make_entry(content_hash=_hash("Hello, World!"))
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_material_fp(entry)
        assert result == known

    async def test_empty_node_known_hash(self) -> None:
        """Empty node (no materials, no children) = sha256 of empty string."""
        known = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        node = _make_node()
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)
        assert result == known

    async def test_node_with_one_material_known_hash(self) -> None:
        """Node with single material = sha256('m:<material_fp>')."""
        content = "test"
        mat_fp = _hash(content)
        expected = _hash(f"m:{mat_fp}")

        node = _make_node(documents=[_make_entry(content_hash=mat_fp)])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)
        assert result == expected


class TestEdgeCases:
    """Edge case tests for fingerprint computation."""

    async def test_empty_string_content(self) -> None:
        """Empty string content_hash produces valid fingerprint."""
        entry = _make_entry(content_hash=_hash(""))
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_material_fp(entry)

        expected = _hash(b"")
        assert result == expected
        assert len(result) == 64

    async def test_unicode_content(self) -> None:
        """Unicode content (Cyrillic, emoji) hashed correctly."""
        content = "Привіт, 世界! 🎓"
        entry = _make_entry(content_hash=_hash(content))
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_material_fp(entry)

        expected = _hash(content)
        assert result == expected

    async def test_very_deep_tree(self) -> None:
        """Fingerprint propagates through 10-level deep tree."""
        session = AsyncMock()
        svc = FingerprintService(session)

        # Build chain: leaf → ... → root (10 levels)
        leaf = _make_node(documents=[_make_entry(content_hash=_hash("deep"))])
        current = leaf
        for _ in range(9):
            current = _make_node(children=[current])
        root = current

        result = await svc.ensure_node_fp(root)

        assert len(result) == 64
        assert root.content_hash == result
        # Verify all intermediate nodes got fingerprints
        node = root
        for _ in range(10):
            assert node.content_hash is not None
            if node.children:
                node = node.children[0]

    async def test_large_content(self) -> None:
        """Large content_hash produces valid fingerprint."""
        content = "x" * 1_000_000
        entry = _make_entry(content_hash=_hash(content))
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_material_fp(entry)

        expected = _hash(content)
        assert result == expected

    async def test_node_all_materials_unprocessed(self) -> None:
        """Node where all materials lack content_hash = empty hash."""
        raw1 = _make_entry(content_hash=None)
        raw2 = _make_entry(content_hash=None)
        node = _make_node(documents=[raw1, raw2])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        # All materials skipped → same as empty node
        expected = _hash(b"")
        assert result == expected


class TestBranchIndependence:
    """Verify changes in one branch don't affect another."""

    async def test_invalidation_preserves_other_branch(self) -> None:
        """Invalidating one branch leaves sibling branch fingerprints intact."""
        # Tree:
        #        root
        #       /    \
        #    branchA  branchB
        #      |        |
        #    leafA    leafB
        leaf_a = _make_node(
            documents=[_make_entry(content_hash=_hash("A"))],
            content_hash="leaf_a_fp",
        )
        leaf_b = _make_node(
            documents=[_make_entry(content_hash=_hash("B"))],
            content_hash="leaf_b_fp",
        )
        branch_a = _make_node(children=[leaf_a], content_hash="branch_a_fp")
        branch_b = _make_node(children=[leaf_b], content_hash="branch_b_fp")
        root = _make_node(children=[branch_a, branch_b], content_hash="root_fp")

        leaf_a.parent_id = branch_a.id
        branch_a.parent_id = root.id
        leaf_b.parent_id = branch_b.id
        branch_b.parent_id = root.id
        root.parent_id = None

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda _cls, pid: {
                branch_a.id: branch_a,
                branch_b.id: branch_b,
                root.id: root,
            }.get(pid)
        )

        svc = FingerprintService(session)
        await svc.invalidate_up(leaf_a)

        # Branch A path invalidated
        assert leaf_a.content_hash is None
        assert branch_a.content_hash is None
        assert root.content_hash is None

        # Branch B untouched
        assert leaf_b.content_hash == "leaf_b_fp"
        assert branch_b.content_hash == "branch_b_fp"

    async def test_different_branches_produce_different_hashes(self) -> None:
        """Two branches with different content have different fingerprints."""
        session = AsyncMock()
        svc = FingerprintService(session)

        branch_a = _make_node(documents=[_make_entry(content_hash=_hash("alpha"))])
        branch_b = _make_node(documents=[_make_entry(content_hash=_hash("beta"))])

        fp_a = await svc.ensure_node_fp(branch_a)
        fp_b = await svc.ensure_node_fp(branch_b)

        assert fp_a != fp_b

    async def test_swapping_branches_changes_nothing(self) -> None:
        """Swapping branch order doesn't change root fp (sorted parts)."""
        session = AsyncMock()
        svc = FingerprintService(session)

        root1 = _make_node(
            children=[
                _make_node(documents=[_make_entry(content_hash=_hash("A"))]),
                _make_node(documents=[_make_entry(content_hash=_hash("B"))]),
            ]
        )
        root2 = _make_node(
            children=[
                _make_node(documents=[_make_entry(content_hash=_hash("B"))]),
                _make_node(documents=[_make_entry(content_hash=_hash("A"))]),
            ]
        )

        fp1 = await svc.ensure_node_fp(root1)
        fp2 = await svc.ensure_node_fp(root2)
        assert fp1 == fp2


class TestLazyCalculation:
    """Verify fingerprints are only computed when needed."""

    async def test_cached_subtree_not_recomputed(self) -> None:
        """Child with cached fingerprint is not recomputed."""
        child_fp = "c" * 64
        child = _make_node(content_hash=child_fp)
        parent = _make_node(children=[child])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(parent)

        # Parent computed using child's cached fp
        expected = _hash(f"n:{child_fp}")
        assert result == expected
        # Child's fingerprint wasn't changed
        assert child.content_hash == child_fp

    async def test_mixed_cached_and_fresh(self) -> None:
        """Node with one cached child and one fresh child works correctly."""
        cached_fp = "d" * 64
        cached_child = _make_node(content_hash=cached_fp)
        fresh_child = _make_node(documents=[_make_entry(content_hash=_hash("new"))])
        parent = _make_node(children=[cached_child, fresh_child])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(parent)

        # Fresh child gets computed
        fresh_fp = _hash(f"m:{_hash('new')}")
        parts = sorted([f"n:{cached_fp}", f"n:{fresh_fp}"])
        expected = _hash("\n".join(parts))
        assert result == expected

    async def test_cached_material_uses_processed_hash(self) -> None:
        """Material fingerprint comes from content_hash directly."""
        cached_fp = "e" * 64
        mat = _make_entry(content_hash=cached_fp)
        node = _make_node(documents=[mat])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        expected = _hash(f"m:{cached_fp}")
        assert result == expected

    async def test_ensure_course_fp_uses_cached_nodes(self) -> None:
        """ensure_course_fp does not recompute cached root nodes."""
        cached_fp = "f" * 64
        root = _make_node(content_hash=cached_fp)
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_course_fp([root])

        expected = _hash(cached_fp)
        assert result == expected

    async def test_no_flush_when_all_cached(self) -> None:
        """ensure_node_fp with fully cached node does not flush."""
        node = _make_node(content_hash="a" * 64)
        session = AsyncMock()
        svc = FingerprintService(session)

        await svc.ensure_node_fp(node)
        session.flush.assert_not_awaited()


class TestCourseFpDeepTree:
    """Course fingerprint with deep, complex trees."""

    async def test_course_fp_with_deep_nested_tree(self) -> None:
        """Course fp reflects entire nested tree structure."""
        session = AsyncMock()
        svc = FingerprintService(session)

        # Root → child → grandchild with material
        grandchild = _make_node(documents=[_make_entry(content_hash=_hash("deep"))])
        child = _make_node(children=[grandchild])
        root = _make_node(children=[child])

        fp1 = await svc.ensure_course_fp([root])

        # Change the deep material
        grandchild2 = _make_node(documents=[_make_entry(content_hash=_hash("changed"))])
        child2 = _make_node(children=[grandchild2])
        root2 = _make_node(children=[child2])

        fp2 = await svc.ensure_course_fp([root2])

        assert fp1 != fp2

    async def test_course_fp_multiple_roots_with_subtrees(self) -> None:
        """Course with multiple root nodes, each with subtrees."""
        session = AsyncMock()
        svc = FingerprintService(session)

        root_a = _make_node(
            children=[
                _make_node(documents=[_make_entry(content_hash=_hash("a1"))]),
                _make_node(documents=[_make_entry(content_hash=_hash("a2"))]),
            ]
        )
        root_b = _make_node(documents=[_make_entry(content_hash=_hash("b"))])

        result = await svc.ensure_course_fp([root_a, root_b])

        assert len(result) == 64
        assert isinstance(result, str)
