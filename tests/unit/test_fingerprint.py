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
