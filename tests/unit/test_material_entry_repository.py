"""Tests for MaterialEntryRepository."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.storage.material_entry_repository import MaterialEntryRepository
from course_supporter.storage.orm import MaterialEntry


@pytest.fixture(autouse=True)
def _no_cascade_invalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable fingerprint cascade invalidation in unit tests."""
    monkeypatch.setattr(
        MaterialEntryRepository,
        "_invalidate_node_chain",
        AsyncMock(),
    )


def _mock_entry(
    *,
    entry_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    source_type: str = "web",
    source_url: str = "https://example.com",
    filename: str | None = None,
    order: int = 0,
    raw_hash: str | None = None,
    raw_size_bytes: int | None = None,
    processed_content: str | None = None,
    processed_hash: str | None = None,
    processed_at: datetime | None = None,
    pending_job_id: uuid.UUID | None = None,
    pending_since: datetime | None = None,
    error_message: str | None = None,
) -> MagicMock:
    """Create a mock MaterialEntry."""
    entry = MagicMock(spec=MaterialEntry)
    entry.id = entry_id or uuid.uuid4()
    entry.node_id = node_id or uuid.uuid4()
    entry.source_type = source_type
    entry.source_url = source_url
    entry.filename = filename
    entry.order = order
    entry.raw_hash = raw_hash
    entry.raw_size_bytes = raw_size_bytes
    entry.processed_content = processed_content
    entry.processed_hash = processed_hash
    entry.processed_at = processed_at
    entry.pending_job_id = pending_job_id
    entry.pending_since = pending_since
    entry.error_message = error_message
    entry.content_fingerprint = None
    return entry


class TestCreate:
    """MaterialEntryRepository.create tests."""

    async def test_create_entry(self) -> None:
        """Entry created with auto-incremented order."""
        session = AsyncMock()
        session.add = MagicMock()
        repo = MaterialEntryRepository(session)

        node_id = uuid.uuid4()

        with patch.object(repo, "_next_sibling_order", return_value=0):
            result = await repo.create(
                node_id=node_id,
                source_type="web",
                source_url="https://example.com/article",
            )

        session.add.assert_called_once()
        session.flush.assert_awaited()
        added = session.add.call_args[0][0]
        assert isinstance(added, MaterialEntry)
        assert added.node_id == node_id
        assert added.source_type == "web"
        assert added.source_url == "https://example.com/article"
        assert added.filename is None
        assert added.order == 0
        assert result is added

    async def test_create_with_filename(self) -> None:
        """Entry created with optional filename."""
        session = AsyncMock()
        session.add = MagicMock()
        repo = MaterialEntryRepository(session)

        with patch.object(repo, "_next_sibling_order", return_value=2):
            result = await repo.create(
                node_id=uuid.uuid4(),
                source_type="video",
                source_url="s3://bucket/lecture.mp4",
                filename="lecture-1.mp4",
            )

        added = session.add.call_args[0][0]
        assert added.filename == "lecture-1.mp4"
        assert added.order == 2
        assert result is added


class TestGetById:
    """MaterialEntryRepository.get_by_id tests."""

    async def test_found(self) -> None:
        """Returns entry when found."""
        entry = _mock_entry()
        session = AsyncMock()
        session.get.return_value = entry

        repo = MaterialEntryRepository(session)
        result = await repo.get_by_id(entry.id)
        assert result is entry

    async def test_not_found(self) -> None:
        """Returns None when not found."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialEntryRepository(session)
        result = await repo.get_by_id(uuid.uuid4())
        assert result is None


class TestGetForNode:
    """MaterialEntryRepository.get_for_node tests."""

    async def test_returns_entries_ordered(self) -> None:
        """Returns entries ordered by position."""
        nid = uuid.uuid4()
        e0 = _mock_entry(node_id=nid, order=0)
        e1 = _mock_entry(node_id=nid, order=1)

        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [e0, e1]
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        session.execute.return_value = result_mock

        repo = MaterialEntryRepository(session)
        result = await repo.get_for_node(nid)

        assert len(result) == 2
        assert result[0] is e0
        assert result[1] is e1

    async def test_filter_by_source_type(self) -> None:
        """Filters by source_type when provided."""
        nid = uuid.uuid4()
        e0 = _mock_entry(node_id=nid, source_type="web")

        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [e0]
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        session.execute.return_value = result_mock

        repo = MaterialEntryRepository(session)
        result = await repo.get_for_node(nid, source_type="web")

        assert len(result) == 1
        assert result[0] is e0

    async def test_empty_node(self) -> None:
        """Returns empty list for node with no entries."""
        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        session.execute.return_value = result_mock

        repo = MaterialEntryRepository(session)
        result = await repo.get_for_node(uuid.uuid4())
        assert result == []


class TestSetPending:
    """MaterialEntryRepository.set_pending tests."""

    async def test_sets_pending_fields(self) -> None:
        """Sets pending_job_id, pending_since, clears error_message."""
        entry = _mock_entry(error_message="old error")
        session = AsyncMock()
        session.get.return_value = entry

        repo = MaterialEntryRepository(session)
        job_id = uuid.uuid4()
        now = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
        result = await repo.set_pending(entry.id, job_id, now=now)

        assert result.pending_job_id == job_id
        assert result.pending_since == now
        assert result.error_message is None
        session.flush.assert_awaited()

    async def test_not_found(self) -> None:
        """ValueError if entry doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialEntryRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.set_pending(uuid.uuid4(), uuid.uuid4())

    async def test_uses_utc_now_by_default(self) -> None:
        """Uses current UTC time when now is not provided."""
        entry = _mock_entry()
        session = AsyncMock()
        session.get.return_value = entry

        repo = MaterialEntryRepository(session)
        before = datetime.now(UTC)
        await repo.set_pending(entry.id, uuid.uuid4())
        after = datetime.now(UTC)

        assert before <= entry.pending_since <= after


class TestCompleteProcessing:
    """MaterialEntryRepository.complete_processing tests."""

    async def test_completes_successfully(self) -> None:
        """Sets processed fields and clears pending receipt."""
        job_id = uuid.uuid4()
        entry = _mock_entry(
            pending_job_id=job_id,
            pending_since=datetime(2026, 1, 1, tzinfo=UTC),
            error_message="old",
        )
        session = AsyncMock()
        session.get.return_value = entry

        repo = MaterialEntryRepository(session)
        now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        result = await repo.complete_processing(
            entry.id,
            processed_content='{"sections": []}',
            processed_hash="a" * 64,
            now=now,
        )

        assert result.processed_content == '{"sections": []}'
        assert result.processed_hash == "a" * 64
        assert result.processed_at == now
        assert result.pending_job_id is None
        assert result.pending_since is None
        assert result.error_message is None
        session.flush.assert_awaited()

    async def test_not_found(self) -> None:
        """ValueError if entry doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialEntryRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.complete_processing(
                uuid.uuid4(),
                processed_content="text",
                processed_hash="a" * 64,
            )


class TestFailProcessing:
    """MaterialEntryRepository.fail_processing tests."""

    async def test_fails_with_error(self) -> None:
        """Sets error_message and clears pending receipt."""
        entry = _mock_entry(
            pending_job_id=uuid.uuid4(),
            pending_since=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session = AsyncMock()
        session.get.return_value = entry

        repo = MaterialEntryRepository(session)
        result = await repo.fail_processing(
            entry.id,
            error_message="LLM timeout",
        )

        assert result.error_message == "LLM timeout"
        assert result.pending_job_id is None
        assert result.pending_since is None
        session.flush.assert_awaited()

    async def test_not_found(self) -> None:
        """ValueError if entry doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialEntryRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.fail_processing(uuid.uuid4(), error_message="fail")


class TestUpdateSource:
    """MaterialEntryRepository.update_source tests."""

    async def test_updates_url_and_invalidates_hash(self) -> None:
        """Updates source_url and clears raw_hash/raw_size_bytes."""
        entry = _mock_entry(
            source_url="https://old.com",
            raw_hash="a" * 64,
            raw_size_bytes=1024,
        )
        session = AsyncMock()
        session.get.return_value = entry

        repo = MaterialEntryRepository(session)
        result = await repo.update_source(
            entry.id,
            source_url="https://new.com/article",
            filename="new.html",
        )

        assert result.source_url == "https://new.com/article"
        assert result.filename == "new.html"
        assert result.raw_hash is None
        assert result.raw_size_bytes is None
        session.flush.assert_awaited()

    async def test_clears_filename_by_default(self) -> None:
        """Filename cleared when not provided."""
        entry = _mock_entry(filename="old.pdf")
        session = AsyncMock()
        session.get.return_value = entry

        repo = MaterialEntryRepository(session)
        result = await repo.update_source(
            entry.id,
            source_url="https://new.com",
        )

        assert result.filename is None

    async def test_not_found(self) -> None:
        """ValueError if entry doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialEntryRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.update_source(uuid.uuid4(), source_url="https://x.com")


class TestEnsureRawHash:
    """MaterialEntryRepository.ensure_raw_hash tests."""

    async def test_sets_hash_when_none(self) -> None:
        """Computes and sets raw_hash from bytes."""
        entry = _mock_entry(raw_hash=None, raw_size_bytes=None)
        session = AsyncMock()
        session.get.return_value = entry

        repo = MaterialEntryRepository(session)
        raw = b"hello world"
        result = await repo.ensure_raw_hash(entry.id, raw_bytes=raw)

        import hashlib

        expected = hashlib.sha256(raw).hexdigest()
        assert result.raw_hash == expected
        assert result.raw_size_bytes == len(raw)
        session.flush.assert_awaited()

    async def test_skips_when_already_set(self) -> None:
        """Does not overwrite existing raw_hash."""
        existing_hash = "b" * 64
        entry = _mock_entry(raw_hash=existing_hash, raw_size_bytes=512)
        session = AsyncMock()
        session.get.return_value = entry

        repo = MaterialEntryRepository(session)
        result = await repo.ensure_raw_hash(entry.id, raw_bytes=b"new data")

        assert result.raw_hash == existing_hash
        assert result.raw_size_bytes == 512
        session.flush.assert_not_awaited()

    async def test_not_found(self) -> None:
        """ValueError if entry doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialEntryRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.ensure_raw_hash(uuid.uuid4(), raw_bytes=b"data")


class TestDelete:
    """MaterialEntryRepository.delete tests."""

    async def test_delete_calls_session_delete(self) -> None:
        """Delegates to session.delete + flush."""
        entry = _mock_entry()
        session = AsyncMock()
        session.get.return_value = entry

        repo = MaterialEntryRepository(session)
        await repo.delete(entry.id)

        session.delete.assert_awaited_once_with(entry)
        session.flush.assert_awaited()

    async def test_delete_not_found(self) -> None:
        """ValueError if entry doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialEntryRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.delete(uuid.uuid4())


class TestLifecycle:
    """Full lifecycle: RAW → PENDING → READY."""

    async def test_raw_to_pending_to_ready(self) -> None:
        """Full success lifecycle through repository operations."""
        # Start with a RAW entry (real ORM object for state property)
        from course_supporter.storage.orm import _uuid7

        node_id = _uuid7()
        entry = MaterialEntry(
            node_id=node_id,
            source_type="web",
            source_url="https://example.com",
        )
        assert entry.state.value == "raw"

        # Simulate set_pending
        job_id = _uuid7()
        entry.pending_job_id = job_id
        entry.pending_since = datetime.now(UTC)
        entry.error_message = None
        assert entry.state.value == "pending"

        # Simulate complete_processing
        entry.processed_content = '{"sections": []}'
        entry.processed_hash = "a" * 64
        entry.processed_at = datetime.now(UTC)
        entry.pending_job_id = None
        entry.pending_since = None
        assert entry.state.value == "ready"

    async def test_raw_to_pending_to_error(self) -> None:
        """Failure lifecycle through repository operations."""
        from course_supporter.storage.orm import _uuid7

        entry = MaterialEntry(
            node_id=_uuid7(),
            source_type="text",
            source_url="s3://bucket/notes.md",
        )
        assert entry.state.value == "raw"

        # Simulate set_pending
        entry.pending_job_id = _uuid7()
        entry.pending_since = datetime.now(UTC)
        assert entry.state.value == "pending"

        # Simulate fail_processing
        entry.pending_job_id = None
        entry.pending_since = None
        entry.error_message = "LLM timeout"
        assert entry.state.value == "error"

    async def test_update_source_invalidates(self) -> None:
        """Source update triggers hash invalidation."""
        from course_supporter.storage.orm import _uuid7

        entry = MaterialEntry(
            node_id=_uuid7(),
            source_type="web",
            source_url="https://old.com",
            raw_hash="a" * 64,
            processed_content='{"sections": []}',
            processed_hash="a" * 64,
        )
        assert entry.state.value == "ready"

        # Simulate update_source (invalidate raw_hash)
        entry.source_url = "https://new.com"
        entry.raw_hash = None
        entry.raw_size_bytes = None
        # processed_hash still set but raw_hash is gone
        # State depends on processed_hash vs raw_hash comparison
        # raw_hash is None, processed_hash is set -> READY
        # (raw_hash falsy → skip integrity check)
        assert entry.state.value == "ready"
