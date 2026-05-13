"""Unit tests for AuthoredDocumentRepository — MagicMock-based, fast.

Tests Python flow + signature contract + parameter handling.
Does NOT verify DB behavior, FK constraints, or persistence — those
are covered in ``tests/storage/test_authored_document_repository.py``
(real-DB, Amendment 33-aligned).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.orm import AuthoredDocument


@pytest.fixture(autouse=True)
def _no_cascade_invalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable content_hash cascade invalidation in unit tests.

    Two entry points masked: (1) ``_invalidate_node_chain`` helper used by
    ``update_source`` / legacy ``create`` flows; (2) direct
    ``ContentHashService.invalidate_up`` call wired into ``create()``
    post-flush per Phase 1.1 etap 1.1.4 (variant (a) of §6.7.1). Unit
    tests focus on signature / Python flow; the canonical KD9 walker is
    exercised in ``tests/integration/test_content_hash_persistence.py``.
    Tests asserting these specific callsites override per-test via
    ``mp.setattr`` (see ``TestInvalidationContract`` below).
    """
    from course_supporter.storage.content_hash import ContentHashService

    monkeypatch.setattr(
        AuthoredDocumentRepository,
        "_invalidate_node_chain",
        AsyncMock(),
    )
    monkeypatch.setattr(
        ContentHashService,
        "invalidate_up",
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
    processed_at: datetime | None = None,
    job_id: uuid.UUID | None = None,
    pending_since: datetime | None = None,
    error_message: str | None = None,
) -> MagicMock:
    """Create a mock AuthoredDocument."""
    entry = MagicMock(spec=AuthoredDocument)
    entry.id = entry_id or uuid.uuid4()
    entry.course_node_id = node_id or uuid.uuid4()
    entry.source_type = source_type
    entry.material_role = "educational"
    entry.source_url = source_url
    entry.filename = filename
    entry.order = order
    entry.raw_hash = raw_hash
    entry.raw_size_bytes = raw_size_bytes
    entry.processed_at = processed_at
    entry.job_id = job_id
    entry.pending_since = pending_since
    entry.error_message = error_message
    return entry


class TestCreate:
    """AuthoredDocumentRepository.create tests."""

    async def test_create_entry(self) -> None:
        """Entry created with auto-incremented order."""
        session = AsyncMock()
        session.add = MagicMock()
        repo = AuthoredDocumentRepository(session)

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
        assert isinstance(added, AuthoredDocument)
        assert added.course_node_id == node_id
        assert added.source_type == "web"
        assert added.source_url == "https://example.com/article"
        assert added.filename is None
        assert added.order == 0
        assert result is added

    async def test_create_with_filename(self) -> None:
        """Entry created with optional filename."""
        session = AsyncMock()
        session.add = MagicMock()
        repo = AuthoredDocumentRepository(session)

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

    async def test_create_without_task_type(self) -> None:
        """task_type defaults to None on regular materials."""
        session = AsyncMock()
        session.add = MagicMock()
        repo = AuthoredDocumentRepository(session)

        with patch.object(repo, "_next_sibling_order", return_value=0):
            await repo.create(
                node_id=uuid.uuid4(),
                source_type="text",
                source_url="s3://bucket/article.md",
            )

        added = session.add.call_args[0][0]
        assert added.task_type is None

    async def test_create_with_task_type_enum(self) -> None:
        """AssignmentType is persisted as its string value."""
        from course_supporter.models.source import AssignmentType

        session = AsyncMock()
        session.add = MagicMock()
        repo = AuthoredDocumentRepository(session)

        with patch.object(repo, "_next_sibling_order", return_value=0):
            await repo.create(
                node_id=uuid.uuid4(),
                source_type="text",
                source_url="s3://bucket/hw1.md",
                task_type=AssignmentType.SHORT_TASK,
            )

        added = session.add.call_args[0][0]
        assert added.task_type == "short_task"

    async def test_create_with_task_type_string(self) -> None:
        """Raw string values are accepted for task_type."""
        session = AsyncMock()
        session.add = MagicMock()
        repo = AuthoredDocumentRepository(session)

        with patch.object(repo, "_next_sibling_order", return_value=0):
            await repo.create(
                node_id=uuid.uuid4(),
                source_type="text",
                source_url="s3://bucket/hw1.md",
                task_type="task",
            )

        added = session.add.call_args[0][0]
        assert added.task_type == "task"


class TestUpdateTaskType:
    """AuthoredDocumentRepository.update_task_type tests."""

    async def test_set_task_type(self) -> None:
        from course_supporter.models.source import AssignmentType

        session = AsyncMock()
        repo = AuthoredDocumentRepository(session)
        entry = _mock_entry()
        entry.task_type = None

        result = await repo.update_task_type(entry, task_type=AssignmentType.PROJECT)

        assert result.task_type == "project"
        session.flush.assert_awaited()

    async def test_clear_task_type(self) -> None:
        session = AsyncMock()
        repo = AuthoredDocumentRepository(session)
        entry = _mock_entry()
        entry.task_type = "task"

        result = await repo.update_task_type(entry, task_type=None)

        assert result.task_type is None
        session.flush.assert_awaited()


class TestGetById:
    """AuthoredDocumentRepository.get_by_id tests."""

    async def test_found(self) -> None:
        """Returns entry when found."""
        entry = _mock_entry()
        session = AsyncMock()
        session.get.return_value = entry

        repo = AuthoredDocumentRepository(session)
        result = await repo.get_by_id(entry.id)
        assert result is entry

    async def test_not_found(self) -> None:
        """Returns None when not found."""
        session = AsyncMock()
        session.get.return_value = None

        repo = AuthoredDocumentRepository(session)
        result = await repo.get_by_id(uuid.uuid4())
        assert result is None


class TestGetForNode:
    """AuthoredDocumentRepository.get_for_node tests."""

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

        repo = AuthoredDocumentRepository(session)
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

        repo = AuthoredDocumentRepository(session)
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

        repo = AuthoredDocumentRepository(session)
        result = await repo.get_for_node(uuid.uuid4())
        assert result == []


class TestSetPending:
    """AuthoredDocumentRepository.set_pending tests."""

    async def test_sets_pending_fields(self) -> None:
        """Sets job_id, pending_since, clears error_message."""
        entry = _mock_entry(error_message="old error")
        session = AsyncMock()
        session.get.return_value = entry

        repo = AuthoredDocumentRepository(session)
        job_id = uuid.uuid4()
        now = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
        result = await repo.set_pending(entry.id, job_id, now=now)

        assert result.job_id == job_id
        assert result.pending_since == now
        assert result.error_message is None
        session.flush.assert_awaited()

    async def test_not_found(self) -> None:
        """ValueError if entry doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = AuthoredDocumentRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.set_pending(uuid.uuid4(), uuid.uuid4())

    async def test_uses_utc_now_by_default(self) -> None:
        """Uses current UTC time when now is not provided."""
        entry = _mock_entry()
        session = AsyncMock()
        session.get.return_value = entry

        repo = AuthoredDocumentRepository(session)
        before = datetime.now(UTC)
        await repo.set_pending(entry.id, uuid.uuid4())
        after = datetime.now(UTC)

        assert before <= entry.pending_since <= after


class TestFailProcessing:
    """AuthoredDocumentRepository.fail_processing tests."""

    async def test_fails_with_error(self) -> None:
        """Sets error_message and clears pending receipt."""
        entry = _mock_entry(
            job_id=uuid.uuid4(),
            pending_since=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session = AsyncMock()
        session.get.return_value = entry

        repo = AuthoredDocumentRepository(session)
        result = await repo.fail_processing(
            entry.id,
            error_message="LLM timeout",
        )

        assert result.error_message == "LLM timeout"
        assert result.job_id is None
        assert result.pending_since is None
        session.flush.assert_awaited()

    async def test_not_found(self) -> None:
        """ValueError if entry doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = AuthoredDocumentRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.fail_processing(uuid.uuid4(), error_message="fail")


class TestUpdateSource:
    """AuthoredDocumentRepository.update_source tests."""

    async def test_updates_url_and_invalidates_hash(self) -> None:
        """Updates source_url and clears raw_hash/raw_size_bytes."""
        entry = _mock_entry(
            source_url="https://old.com",
            raw_hash="a" * 64,
            raw_size_bytes=1024,
        )
        session = AsyncMock()
        session.get.return_value = entry

        repo = AuthoredDocumentRepository(session)
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

        repo = AuthoredDocumentRepository(session)
        result = await repo.update_source(
            entry.id,
            source_url="https://new.com",
        )

        assert result.filename is None

    async def test_not_found(self) -> None:
        """ValueError if entry doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = AuthoredDocumentRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.update_source(uuid.uuid4(), source_url="https://x.com")


class TestEnsureRawHash:
    """AuthoredDocumentRepository.ensure_raw_hash tests."""

    async def test_sets_hash_when_none(self) -> None:
        """Computes and sets raw_hash from bytes."""
        entry = _mock_entry(raw_hash=None, raw_size_bytes=None)
        session = AsyncMock()
        session.get.return_value = entry

        repo = AuthoredDocumentRepository(session)
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

        repo = AuthoredDocumentRepository(session)
        result = await repo.ensure_raw_hash(entry.id, raw_bytes=b"new data")

        assert result.raw_hash == existing_hash
        assert result.raw_size_bytes == 512
        session.flush.assert_not_awaited()

    async def test_not_found(self) -> None:
        """ValueError if entry doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = AuthoredDocumentRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.ensure_raw_hash(uuid.uuid4(), raw_bytes=b"data")


class TestInvalidationContract:
    """Asserts the parent-chain invalidation contract preserved post-Phase-1.1.

    Bypasses the file-level ``_no_cascade_invalidation`` autouse fixture
    via per-test ``mp.setattr`` instance-level override — the autouse mock
    masks the helper for every other test, but here we explicitly observe
    the call surface to lock the contract.

    Migrated from the legacy
    ``tests/unit/test_fingerprint.py::TestRepositoryInvalidation`` before
    that file's wholesale deletion (Phase 1.1 etap 1.1.3). The mock
    target is the helper itself (``_invalidate_node_chain``), so the body
    rewire from the legacy fingerprint service to ``ContentHashService`` is
    invisible at this layer — what matters is that ``update_source`` triggers
    it and ``complete_processing`` does not.
    """

    async def test_update_source_invalidates_node_chain(self) -> None:
        """``update_source`` triggers parent chain invalidation post-flush."""
        entry = _mock_entry(
            source_url="https://old.com",
            raw_hash="a" * 64,
            raw_size_bytes=1024,
        )
        session = AsyncMock()
        session.get.return_value = entry

        repo = AuthoredDocumentRepository(session)
        invalidate_mock = AsyncMock()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_invalidate_node_chain", invalidate_mock)
            await repo.update_source(
                entry.id,
                source_url="https://new-url.com",
            )

        invalidate_mock.assert_awaited_once_with(entry.course_node_id)

    async def test_complete_processing_does_not_invalidate_node_chain(
        self,
    ) -> None:
        """``complete_processing`` is intentionally state-transition-only.

        Per Amendment 35 sub-class 3d (intentional non-behavior verification
        — hotfix-9 design): ``complete_processing`` clears ``job_id`` /
        ``pending_since`` / ``error_message`` and sets ``processed_at``, but
        does NOT cascade invalidate parent hashes. Cascade invalidation
        belongs to ``update_source`` (raw bytes change) and ``create``
        (initial insertion); state transitions alone do not affect
        ``content_hash``. This negative assertion serves as regression guard
        — re-adding cascade invalidation here without review would be caught.
        """
        entry = MagicMock(spec=AuthoredDocument)

        session = AsyncMock()
        repo = AuthoredDocumentRepository(session)
        invalidate_mock = AsyncMock()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_require", AsyncMock(return_value=entry))
            mp.setattr(repo, "_invalidate_node_chain", invalidate_mock)
            await repo.complete_processing(entry.id)

        invalidate_mock.assert_not_awaited()
