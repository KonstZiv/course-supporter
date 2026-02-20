"""Tests for FingerprintService — material & node levels (S2-024, S2-025)."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.fingerprint import FingerprintService
from course_supporter.storage.orm import MaterialEntry, MaterialNode


def _make_entry(
    *,
    processed_content: str | None = None,
    content_fingerprint: str | None = None,
) -> MagicMock:
    """Create a mock MaterialEntry with the specified fields."""
    entry = MagicMock(spec=MaterialEntry)
    entry.id = uuid.uuid4()
    entry.processed_content = processed_content
    entry.content_fingerprint = content_fingerprint
    return entry


def _make_node(
    *,
    materials: list[MagicMock] | None = None,
    children: list[MagicMock] | None = None,
    node_fingerprint: str | None = None,
) -> MagicMock:
    """Create a mock MaterialNode with materials and children."""
    node = MagicMock(spec=MaterialNode)
    node.id = uuid.uuid4()
    node.materials = materials or []
    node.children = children or []
    node.node_fingerprint = node_fingerprint
    return node


class TestEnsureMaterialFp:
    async def test_computes_sha256_of_processed_content(self) -> None:
        """Fingerprint is sha256 hex digest of processed_content bytes."""
        content = "Hello, this is processed content."
        entry = _make_entry(processed_content=content)
        session = AsyncMock()

        svc = FingerprintService(session)
        result = await svc.ensure_material_fp(entry)

        expected = hashlib.sha256(content.encode()).hexdigest()
        assert result == expected
        assert entry.content_fingerprint == expected
        session.flush.assert_awaited_once()

    async def test_cache_hit_returns_existing(self) -> None:
        """If content_fingerprint is already set, return it without flush."""
        cached = "a" * 64
        entry = _make_entry(
            processed_content="some content",
            content_fingerprint=cached,
        )
        session = AsyncMock()

        svc = FingerprintService(session)
        result = await svc.ensure_material_fp(entry)

        assert result == cached
        session.flush.assert_not_awaited()

    async def test_invalidation_then_recalculate(self) -> None:
        """After clearing fingerprint (None), next call recomputes."""
        content = "original content"
        fp = hashlib.sha256(content.encode()).hexdigest()
        entry = _make_entry(processed_content=content, content_fingerprint=fp)
        session = AsyncMock()
        svc = FingerprintService(session)

        # Cache hit — no flush
        result1 = await svc.ensure_material_fp(entry)
        assert result1 == fp
        session.flush.assert_not_awaited()

        # Invalidate
        entry.content_fingerprint = None

        # Recalculate
        result2 = await svc.ensure_material_fp(entry)
        assert result2 == fp
        session.flush.assert_awaited_once()

    async def test_raises_when_no_processed_content(self) -> None:
        """ValueError if processed_content is None."""
        entry = _make_entry(processed_content=None)
        session = AsyncMock()
        svc = FingerprintService(session)

        with pytest.raises(ValueError, match="no processed_content"):
            await svc.ensure_material_fp(entry)

    async def test_deterministic_same_content_same_hash(self) -> None:
        """Same processed_content always produces the same fingerprint."""
        content = "deterministic test"
        session = AsyncMock()
        svc = FingerprintService(session)

        entry1 = _make_entry(processed_content=content)
        entry2 = _make_entry(processed_content=content)

        fp1 = await svc.ensure_material_fp(entry1)
        fp2 = await svc.ensure_material_fp(entry2)

        assert fp1 == fp2

    async def test_different_content_different_hash(self) -> None:
        """Different processed_content produces different fingerprints."""
        session = AsyncMock()
        svc = FingerprintService(session)

        entry1 = _make_entry(processed_content="content A")
        entry2 = _make_entry(processed_content="content B")

        fp1 = await svc.ensure_material_fp(entry1)
        fp2 = await svc.ensure_material_fp(entry2)

        assert fp1 != fp2

    async def test_fingerprint_is_64_char_hex(self) -> None:
        """Fingerprint is a valid 64-character hex string (sha256)."""
        entry = _make_entry(processed_content="test")
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_material_fp(entry)

        assert len(result) == 64
        int(result, 16)  # valid hex


class TestRepositoryInvalidation:
    """Verify that repository methods clear content_fingerprint."""

    async def test_complete_processing_clears_fingerprint(self) -> None:
        """complete_processing sets content_fingerprint to None."""
        from course_supporter.storage.material_entry_repository import (
            MaterialEntryRepository,
        )

        entry = MagicMock(spec=MaterialEntry)
        entry.content_fingerprint = "old_fp"

        session = AsyncMock()
        repo = MaterialEntryRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_require", AsyncMock(return_value=entry))
            mp.setattr(repo, "_invalidate_node_chain", AsyncMock())
            await repo.complete_processing(
                entry.id,
                processed_content="new content",
                processed_hash="abc123",
            )

        assert entry.content_fingerprint is None

    async def test_update_source_clears_fingerprint(self) -> None:
        """update_source sets content_fingerprint to None."""
        from course_supporter.storage.material_entry_repository import (
            MaterialEntryRepository,
        )

        entry = MagicMock(spec=MaterialEntry)
        entry.content_fingerprint = "old_fp"

        session = AsyncMock()
        repo = MaterialEntryRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_require", AsyncMock(return_value=entry))
            mp.setattr(repo, "_invalidate_node_chain", AsyncMock())
            await repo.update_source(
                entry.id,
                source_url="https://new-url.com",
            )

        assert entry.content_fingerprint is None


class TestEnsureNodeFp:
    """Tests for ensure_node_fp — Merkle hash of a node subtree."""

    async def test_empty_node(self) -> None:
        """Empty node (no materials, no children) returns deterministic hash."""
        node = _make_node()
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected
        assert node.node_fingerprint == expected
        session.flush.assert_awaited()

    async def test_single_material(self) -> None:
        """Node with one processed material includes its fingerprint."""
        mat = _make_entry(processed_content="lesson text")
        node = _make_node(materials=[mat])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        mat_fp = hashlib.sha256(b"lesson text").hexdigest()
        expected = hashlib.sha256(f"m:{mat_fp}".encode()).hexdigest()
        assert result == expected

    async def test_skips_unprocessed_materials(self) -> None:
        """Materials without processed_content are excluded."""
        processed = _make_entry(processed_content="done")
        raw = _make_entry(processed_content=None)
        node = _make_node(materials=[processed, raw])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        # Same as node with only the processed material
        node_single = _make_node(materials=[_make_entry(processed_content="done")])
        result_single = await svc.ensure_node_fp(node_single)
        assert result == result_single

    async def test_single_child_node(self) -> None:
        """Node with one child includes child's Merkle hash."""
        child = _make_node(materials=[_make_entry(processed_content="child text")])
        parent = _make_node(children=[child])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(parent)

        # Child fp first
        child_mat_fp = hashlib.sha256(b"child text").hexdigest()
        child_fp = hashlib.sha256(f"m:{child_mat_fp}".encode()).hexdigest()
        expected = hashlib.sha256(f"n:{child_fp}".encode()).hexdigest()
        assert result == expected

    async def test_nested_3_levels(self) -> None:
        """Merkle hash propagates correctly through 3 levels."""
        leaf = _make_node(materials=[_make_entry(processed_content="leaf")])
        mid = _make_node(children=[leaf])
        root = _make_node(children=[mid])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(root)

        # Compute bottom-up
        leaf_mat_fp = hashlib.sha256(b"leaf").hexdigest()
        leaf_fp = hashlib.sha256(f"m:{leaf_mat_fp}".encode()).hexdigest()
        mid_fp = hashlib.sha256(f"n:{leaf_fp}".encode()).hexdigest()
        expected = hashlib.sha256(f"n:{mid_fp}".encode()).hexdigest()
        assert result == expected

    async def test_deterministic_same_data_same_hash(self) -> None:
        """Same tree structure + content produces the same hash."""
        session = AsyncMock()
        svc = FingerprintService(session)

        node1 = _make_node(materials=[_make_entry(processed_content="aaa")])
        node2 = _make_node(materials=[_make_entry(processed_content="aaa")])

        fp1 = await svc.ensure_node_fp(node1)
        fp2 = await svc.ensure_node_fp(node2)
        assert fp1 == fp2

    async def test_cache_hit_returns_existing(self) -> None:
        """If node_fingerprint is already set, return it without recalc."""
        cached = "b" * 64
        node = _make_node(node_fingerprint=cached)
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        assert result == cached
        session.flush.assert_not_awaited()

    async def test_invalidation_then_recalculate(self) -> None:
        """After clearing node_fingerprint, next call recomputes."""
        node = _make_node(materials=[_make_entry(processed_content="data")])
        session = AsyncMock()
        svc = FingerprintService(session)

        fp1 = await svc.ensure_node_fp(node)
        assert node.node_fingerprint == fp1

        # Invalidate
        node.node_fingerprint = None

        fp2 = await svc.ensure_node_fp(node)
        assert fp2 == fp1  # same data → same hash

    async def test_parts_are_sorted(self) -> None:
        """Material and child parts are sorted before hashing."""
        session = AsyncMock()
        svc = FingerprintService(session)

        mat_a = _make_entry(processed_content="aaa")
        mat_b = _make_entry(processed_content="bbb")

        # Order of materials should not affect fingerprint
        node1 = _make_node(materials=[mat_a, mat_b])
        node2 = _make_node(
            materials=[
                _make_entry(processed_content="bbb"),
                _make_entry(processed_content="aaa"),
            ],
        )

        fp1 = await svc.ensure_node_fp(node1)
        fp2 = await svc.ensure_node_fp(node2)
        assert fp1 == fp2

    async def test_materials_and_children_mixed(self) -> None:
        """Node with both materials and children combines all parts."""
        child = _make_node(materials=[_make_entry(processed_content="child")])
        mat = _make_entry(processed_content="parent mat")
        parent = _make_node(materials=[mat], children=[child])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(parent)

        # Compute expected
        mat_fp = hashlib.sha256(b"parent mat").hexdigest()
        child_mat_fp = hashlib.sha256(b"child").hexdigest()
        child_fp = hashlib.sha256(f"m:{child_mat_fp}".encode()).hexdigest()
        parts = sorted([f"m:{mat_fp}", f"n:{child_fp}"])
        expected = hashlib.sha256("\n".join(parts).encode()).hexdigest()
        assert result == expected


class TestEnsureCourseFp:
    """Tests for ensure_course_fp — course-level Merkle hash."""

    async def test_empty_course_no_roots(self) -> None:
        """Course with no root nodes returns hash of empty string."""
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_course_fp([])

        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected

    async def test_single_root(self) -> None:
        """Course with one root node returns hash of that root's fp."""
        root = _make_node(materials=[_make_entry(processed_content="data")])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_course_fp([root])

        root_fp = hashlib.sha256(
            f"m:{hashlib.sha256(b'data').hexdigest()}".encode()
        ).hexdigest()
        expected = hashlib.sha256(root_fp.encode()).hexdigest()
        assert result == expected

    async def test_multiple_roots_sorted(self) -> None:
        """Root node order does not affect course fingerprint."""
        root_a = _make_node(materials=[_make_entry(processed_content="aaa")])
        root_b = _make_node(materials=[_make_entry(processed_content="bbb")])
        session = AsyncMock()
        svc = FingerprintService(session)

        fp1 = await svc.ensure_course_fp([root_a, root_b])

        # Reverse order
        root_a2 = _make_node(materials=[_make_entry(processed_content="aaa")])
        root_b2 = _make_node(materials=[_make_entry(processed_content="bbb")])
        fp2 = await svc.ensure_course_fp([root_b2, root_a2])

        assert fp1 == fp2

    async def test_stable_when_nothing_changes(self) -> None:
        """Same tree produces same course fingerprint."""
        session = AsyncMock()
        svc = FingerprintService(session)

        root1 = _make_node(materials=[_make_entry(processed_content="x")])
        root2 = _make_node(materials=[_make_entry(processed_content="x")])

        fp1 = await svc.ensure_course_fp([root1])
        fp2 = await svc.ensure_course_fp([root2])
        assert fp1 == fp2

    async def test_changes_when_material_changes(self) -> None:
        """Course fp changes when any material content changes."""
        session = AsyncMock()
        svc = FingerprintService(session)

        root_v1 = _make_node(materials=[_make_entry(processed_content="v1")])
        root_v2 = _make_node(materials=[_make_entry(processed_content="v2")])

        fp1 = await svc.ensure_course_fp([root_v1])
        fp2 = await svc.ensure_course_fp([root_v2])
        assert fp1 != fp2

    async def test_single_flush(self) -> None:
        """ensure_course_fp issues exactly one flush."""
        root = _make_node(
            children=[
                _make_node(materials=[_make_entry(processed_content="a")]),
                _make_node(materials=[_make_entry(processed_content="b")]),
            ],
        )
        session = AsyncMock()
        svc = FingerprintService(session)

        await svc.ensure_course_fp([root])

        session.flush.assert_awaited_once()


class TestInvalidateUp:
    """Tests for invalidate_up — cascade fingerprint invalidation."""

    async def test_leaf_to_root_all_invalidated(self) -> None:
        """All ancestors from leaf to root get node_fingerprint=None."""
        leaf = _make_node(node_fingerprint="leaf_fp")
        mid = _make_node(node_fingerprint="mid_fp")
        root = _make_node(node_fingerprint="root_fp")

        leaf.parent_id = mid.id
        mid.parent_id = root.id
        root.parent_id = None

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda _cls, pid: {mid.id: mid, root.id: root}.get(pid)
        )

        svc = FingerprintService(session)
        await svc.invalidate_up(leaf)

        assert leaf.node_fingerprint is None
        assert mid.node_fingerprint is None
        assert root.node_fingerprint is None

    async def test_root_node_only(self) -> None:
        """Root node (no parent) gets invalidated, no further walk."""
        root = _make_node(node_fingerprint="root_fp")
        root.parent_id = None

        session = AsyncMock()
        svc = FingerprintService(session)
        await svc.invalidate_up(root)

        assert root.node_fingerprint is None
        session.get.assert_not_awaited()

    async def test_siblings_untouched(self) -> None:
        """Sibling nodes are not affected by invalidation."""
        leaf = _make_node(node_fingerprint="leaf_fp")
        sibling = _make_node(node_fingerprint="sibling_fp")
        parent = _make_node(node_fingerprint="parent_fp")

        leaf.parent_id = parent.id
        sibling.parent_id = parent.id
        parent.parent_id = None

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda _cls, pid: {parent.id: parent}.get(pid)
        )

        svc = FingerprintService(session)
        await svc.invalidate_up(leaf)

        assert leaf.node_fingerprint is None
        assert parent.node_fingerprint is None
        assert sibling.node_fingerprint == "sibling_fp"  # untouched

    async def test_single_flush_after_walk(self) -> None:
        """Only one flush after the entire chain walk."""
        leaf = _make_node(node_fingerprint="fp")
        mid = _make_node(node_fingerprint="fp")
        root = _make_node(node_fingerprint="fp")

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
        leaf = _make_node(node_fingerprint="fp")
        mid = _make_node(node_fingerprint=None)  # already invalidated
        root = _make_node(node_fingerprint="root_fp")

        leaf.parent_id = mid.id
        mid.parent_id = root.id
        root.parent_id = None

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda _cls, pid: {mid.id: mid, root.id: root}.get(pid)
        )

        svc = FingerprintService(session)
        await svc.invalidate_up(leaf)

        assert root.node_fingerprint is None  # still reached and cleared


class TestRepositoryCascadeInvalidation:
    """Tests for auto-invalidation in repository CRUD methods (S2-028)."""

    async def test_entry_create_invalidates_node(self) -> None:
        """MaterialEntryRepository.create triggers cascade invalidation."""
        from course_supporter.storage.material_entry_repository import (
            MaterialEntryRepository,
        )

        session = AsyncMock()
        repo = MaterialEntryRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mock_inv = AsyncMock()
            mp.setattr(repo, "_invalidate_node_chain", mock_inv)
            mp.setattr(repo, "_next_sibling_order", AsyncMock(return_value=0))
            await repo.create(
                node_id=uuid.uuid4(),
                source_type="text",
                source_url="https://example.com",
            )

        mock_inv.assert_awaited_once()

    async def test_entry_complete_processing_invalidates_node(self) -> None:
        """MaterialEntryRepository.complete_processing triggers cascade."""
        from course_supporter.storage.material_entry_repository import (
            MaterialEntryRepository,
        )

        entry = MagicMock(spec=MaterialEntry)
        entry.node_id = uuid.uuid4()

        session = AsyncMock()
        repo = MaterialEntryRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_require", AsyncMock(return_value=entry))
            mock_inv = AsyncMock()
            mp.setattr(repo, "_invalidate_node_chain", mock_inv)
            await repo.complete_processing(
                entry.id, processed_content="done", processed_hash="abc"
            )

        mock_inv.assert_awaited_once_with(entry.node_id)

    async def test_entry_update_source_invalidates_node(self) -> None:
        """MaterialEntryRepository.update_source triggers cascade."""
        from course_supporter.storage.material_entry_repository import (
            MaterialEntryRepository,
        )

        entry = MagicMock(spec=MaterialEntry)
        entry.node_id = uuid.uuid4()

        session = AsyncMock()
        repo = MaterialEntryRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_require", AsyncMock(return_value=entry))
            mock_inv = AsyncMock()
            mp.setattr(repo, "_invalidate_node_chain", mock_inv)
            await repo.update_source(entry.id, source_url="https://new.com")

        mock_inv.assert_awaited_once_with(entry.node_id)

    async def test_entry_delete_invalidates_node(self) -> None:
        """MaterialEntryRepository.delete triggers cascade."""
        from course_supporter.storage.material_entry_repository import (
            MaterialEntryRepository,
        )

        entry = MagicMock(spec=MaterialEntry)
        entry.node_id = uuid.uuid4()

        session = AsyncMock()
        repo = MaterialEntryRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_require", AsyncMock(return_value=entry))
            mock_inv = AsyncMock()
            mp.setattr(repo, "_invalidate_node_chain", mock_inv)
            await repo.delete(entry.id)

        mock_inv.assert_awaited_once_with(entry.node_id)

    async def test_node_move_invalidates_old_and_new_parent(self) -> None:
        """MaterialNodeRepository.move invalidates both parent chains."""
        from course_supporter.storage.material_node_repository import (
            MaterialNodeRepository,
        )

        old_parent_id = uuid.uuid4()
        new_parent_id = uuid.uuid4()
        node = MagicMock(spec=MaterialNode)
        node.id = uuid.uuid4()
        node.parent_id = old_parent_id
        node.course_id = uuid.uuid4()

        session = AsyncMock()
        repo = MaterialNodeRepository(session)

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

    async def test_node_delete_invalidates_parent(self) -> None:
        """MaterialNodeRepository.delete invalidates parent chain."""
        from course_supporter.storage.material_node_repository import (
            MaterialNodeRepository,
        )

        parent_id = uuid.uuid4()
        node = MagicMock(spec=MaterialNode)
        node.id = uuid.uuid4()
        node.parent_id = parent_id

        session = AsyncMock()
        repo = MaterialNodeRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_by_id", AsyncMock(return_value=node))
            mock_inv = AsyncMock()
            mp.setattr(repo, "_invalidate_node_chain", mock_inv)
            await repo.delete(node.id)

        mock_inv.assert_awaited_once_with(parent_id)

    async def test_node_delete_root_skips_invalidation(self) -> None:
        """Deleting a root node (parent_id=None) skips invalidation."""
        from course_supporter.storage.material_node_repository import (
            MaterialNodeRepository,
        )

        node = MagicMock(spec=MaterialNode)
        node.id = uuid.uuid4()
        node.parent_id = None

        session = AsyncMock()
        repo = MaterialNodeRepository(session)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_by_id", AsyncMock(return_value=node))
            mock_inv = AsyncMock()
            mp.setattr(repo, "_invalidate_node_chain", mock_inv)
            await repo.delete(node.id)

        # _invalidate_node_chain called with None → returns immediately
        mock_inv.assert_awaited_once_with(None)


class TestKnownHash:
    """Verify Merkle hashes against pre-computed known values."""

    async def test_material_known_sha256(self) -> None:
        """Material fingerprint matches independently computed sha256."""
        known = "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
        entry = _make_entry(processed_content="Hello, World!")
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
        mat_fp = hashlib.sha256(content.encode()).hexdigest()
        expected = hashlib.sha256(f"m:{mat_fp}".encode()).hexdigest()

        node = _make_node(materials=[_make_entry(processed_content=content)])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)
        assert result == expected


class TestEdgeCases:
    """Edge case tests for fingerprint computation."""

    async def test_empty_string_content(self) -> None:
        """Empty string processed_content produces valid fingerprint."""
        entry = _make_entry(processed_content="")
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_material_fp(entry)

        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected
        assert len(result) == 64

    async def test_unicode_content(self) -> None:
        """Unicode processed_content (Cyrillic, emoji) hashed correctly."""
        content = "Привіт, 世界! 🎓"
        entry = _make_entry(processed_content=content)
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_material_fp(entry)

        expected = hashlib.sha256(content.encode()).hexdigest()
        assert result == expected

    async def test_very_deep_tree(self) -> None:
        """Fingerprint propagates through 10-level deep tree."""
        session = AsyncMock()
        svc = FingerprintService(session)

        # Build chain: leaf → ... → root (10 levels)
        leaf = _make_node(materials=[_make_entry(processed_content="deep")])
        current = leaf
        for _ in range(9):
            current = _make_node(children=[current])
        root = current

        result = await svc.ensure_node_fp(root)

        assert len(result) == 64
        assert root.node_fingerprint == result
        # Verify all intermediate nodes got fingerprints
        node = root
        for _ in range(10):
            assert node.node_fingerprint is not None
            if node.children:
                node = node.children[0]

    async def test_large_content(self) -> None:
        """Large processed_content produces valid fingerprint."""
        content = "x" * 1_000_000
        entry = _make_entry(processed_content=content)
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_material_fp(entry)

        expected = hashlib.sha256(content.encode()).hexdigest()
        assert result == expected

    async def test_node_all_materials_unprocessed(self) -> None:
        """Node where all materials lack processed_content = empty hash."""
        raw1 = _make_entry(processed_content=None)
        raw2 = _make_entry(processed_content=None)
        node = _make_node(materials=[raw1, raw2])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        # All materials skipped → same as empty node
        expected = hashlib.sha256(b"").hexdigest()
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
            materials=[_make_entry(processed_content="A")],
            node_fingerprint="leaf_a_fp",
        )
        leaf_b = _make_node(
            materials=[_make_entry(processed_content="B")],
            node_fingerprint="leaf_b_fp",
        )
        branch_a = _make_node(children=[leaf_a], node_fingerprint="branch_a_fp")
        branch_b = _make_node(children=[leaf_b], node_fingerprint="branch_b_fp")
        root = _make_node(children=[branch_a, branch_b], node_fingerprint="root_fp")

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
        assert leaf_a.node_fingerprint is None
        assert branch_a.node_fingerprint is None
        assert root.node_fingerprint is None

        # Branch B untouched
        assert leaf_b.node_fingerprint == "leaf_b_fp"
        assert branch_b.node_fingerprint == "branch_b_fp"

    async def test_different_branches_produce_different_hashes(self) -> None:
        """Two branches with different content have different fingerprints."""
        session = AsyncMock()
        svc = FingerprintService(session)

        branch_a = _make_node(materials=[_make_entry(processed_content="alpha")])
        branch_b = _make_node(materials=[_make_entry(processed_content="beta")])

        fp_a = await svc.ensure_node_fp(branch_a)
        fp_b = await svc.ensure_node_fp(branch_b)

        assert fp_a != fp_b

    async def test_swapping_branches_changes_nothing(self) -> None:
        """Swapping branch order doesn't change root fp (sorted parts)."""
        session = AsyncMock()
        svc = FingerprintService(session)

        root1 = _make_node(
            children=[
                _make_node(materials=[_make_entry(processed_content="A")]),
                _make_node(materials=[_make_entry(processed_content="B")]),
            ]
        )
        root2 = _make_node(
            children=[
                _make_node(materials=[_make_entry(processed_content="B")]),
                _make_node(materials=[_make_entry(processed_content="A")]),
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
        child = _make_node(node_fingerprint=child_fp)
        parent = _make_node(children=[child])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(parent)

        # Parent computed using child's cached fp
        expected = hashlib.sha256(f"n:{child_fp}".encode()).hexdigest()
        assert result == expected
        # Child's fingerprint wasn't changed
        assert child.node_fingerprint == child_fp

    async def test_mixed_cached_and_fresh(self) -> None:
        """Node with one cached child and one fresh child works correctly."""
        cached_fp = "d" * 64
        cached_child = _make_node(node_fingerprint=cached_fp)
        fresh_child = _make_node(materials=[_make_entry(processed_content="new")])
        parent = _make_node(children=[cached_child, fresh_child])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(parent)

        # Fresh child gets computed
        fresh_fp = hashlib.sha256(
            f"m:{hashlib.sha256(b'new').hexdigest()}".encode()
        ).hexdigest()
        parts = sorted([f"n:{cached_fp}", f"n:{fresh_fp}"])
        expected = hashlib.sha256("\n".join(parts).encode()).hexdigest()
        assert result == expected

    async def test_cached_material_not_recomputed(self) -> None:
        """Material with existing content_fingerprint uses cached value."""
        cached_fp = "e" * 64
        mat = _make_entry(
            processed_content="should not be hashed",
            content_fingerprint=cached_fp,
        )
        node = _make_node(materials=[mat])
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_node_fp(node)

        expected = hashlib.sha256(f"m:{cached_fp}".encode()).hexdigest()
        assert result == expected

    async def test_ensure_course_fp_uses_cached_nodes(self) -> None:
        """ensure_course_fp does not recompute cached root nodes."""
        cached_fp = "f" * 64
        root = _make_node(node_fingerprint=cached_fp)
        session = AsyncMock()
        svc = FingerprintService(session)

        result = await svc.ensure_course_fp([root])

        expected = hashlib.sha256(cached_fp.encode()).hexdigest()
        assert result == expected

    async def test_no_flush_when_all_cached(self) -> None:
        """ensure_node_fp with fully cached node does not flush."""
        node = _make_node(node_fingerprint="a" * 64)
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
        grandchild = _make_node(materials=[_make_entry(processed_content="deep")])
        child = _make_node(children=[grandchild])
        root = _make_node(children=[child])

        fp1 = await svc.ensure_course_fp([root])

        # Change the deep material
        grandchild2 = _make_node(materials=[_make_entry(processed_content="changed")])
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
                _make_node(materials=[_make_entry(processed_content="a1")]),
                _make_node(materials=[_make_entry(processed_content="a2")]),
            ]
        )
        root_b = _make_node(materials=[_make_entry(processed_content="b")])

        result = await svc.ensure_course_fp([root_a, root_b])

        assert len(result) == 64
        assert isinstance(result, str)
