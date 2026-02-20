"""Tests for FingerprintService — material level (S2-024)."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.fingerprint import FingerprintService
from course_supporter.storage.orm import MaterialEntry


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
