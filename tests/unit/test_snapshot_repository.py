"""Tests for SnapshotRepository."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from course_supporter.storage.orm import CourseStructureSnapshot
from course_supporter.storage.snapshot_repository import SnapshotRepository


def _mock_snapshot(
    *,
    snapshot_id: uuid.UUID | None = None,
    course_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    node_fingerprint: str = "abc123",
    mode: str = "free",
    structure: dict[str, object] | None = None,
) -> MagicMock:
    """Create a mock CourseStructureSnapshot."""
    snap = MagicMock(spec=CourseStructureSnapshot)
    snap.id = snapshot_id or uuid.uuid4()
    snap.course_id = course_id or uuid.uuid4()
    snap.node_id = node_id
    snap.node_fingerprint = node_fingerprint
    snap.mode = mode
    snap.structure = structure or {"title": "Test"}
    snap.prompt_version = None
    snap.model_id = None
    snap.tokens_in = None
    snap.tokens_out = None
    snap.cost_usd = None
    return snap


def _make_session() -> AsyncMock:
    """Create an AsyncSession mock."""
    session = AsyncMock()
    session.add = MagicMock()
    return session


class TestCreate:
    """SnapshotRepository.create tests."""

    async def test_create_returns_snapshot(self) -> None:
        """create() returns a CourseStructureSnapshot instance."""
        session = _make_session()
        repo = SnapshotRepository(session)

        result = await repo.create(
            course_id=uuid.uuid4(),
            node_fingerprint="abc123",
            mode="free",
            structure={"title": "Test"},
        )

        assert isinstance(result, CourseStructureSnapshot)

    async def test_create_adds_to_session(self) -> None:
        """create() calls session.add()."""
        session = _make_session()
        repo = SnapshotRepository(session)

        await repo.create(
            course_id=uuid.uuid4(),
            node_fingerprint="abc123",
            mode="free",
            structure={"title": "Test"},
        )

        session.add.assert_called_once()

    async def test_create_calls_flush_not_commit(self) -> None:
        """create() calls flush(), never commit()."""
        session = _make_session()
        repo = SnapshotRepository(session)

        await repo.create(
            course_id=uuid.uuid4(),
            node_fingerprint="abc123",
            mode="free",
            structure={"title": "Test"},
        )

        session.flush.assert_awaited_once()
        session.commit.assert_not_awaited()

    async def test_create_with_llm_metadata(self) -> None:
        """create() passes LLM metadata fields to the ORM object."""
        session = _make_session()
        repo = SnapshotRepository(session)

        result = await repo.create(
            course_id=uuid.uuid4(),
            node_fingerprint="abc123",
            mode="guided",
            structure={"title": "Test"},
            prompt_version="v1",
            model_id="gemini-2.0-flash",
            tokens_in=500,
            tokens_out=1200,
            cost_usd=0.003,
        )

        assert result.prompt_version == "v1"
        assert result.model_id == "gemini-2.0-flash"
        assert result.tokens_in == 500
        assert result.tokens_out == 1200
        assert result.cost_usd == 0.003

    async def test_create_with_node_id(self) -> None:
        """create() sets node_id for node-level snapshot."""
        session = _make_session()
        repo = SnapshotRepository(session)
        node_id = uuid.uuid4()

        result = await repo.create(
            course_id=uuid.uuid4(),
            node_id=node_id,
            node_fingerprint="abc123",
            mode="free",
            structure={"title": "Test"},
        )

        assert result.node_id == node_id

    async def test_create_course_level_node_id_none(self) -> None:
        """create() without node_id defaults to None (course-level)."""
        session = _make_session()
        repo = SnapshotRepository(session)

        result = await repo.create(
            course_id=uuid.uuid4(),
            node_fingerprint="abc123",
            mode="free",
            structure={"title": "Test"},
        )

        assert result.node_id is None


class TestGetById:
    """SnapshotRepository.get_by_id tests."""

    async def test_get_by_id_found(self) -> None:
        """get_by_id() returns snapshot when found."""
        session = _make_session()
        snap = _mock_snapshot()
        session.get.return_value = snap
        repo = SnapshotRepository(session)

        result = await repo.get_by_id(snap.id)

        assert result is snap

    async def test_get_by_id_not_found(self) -> None:
        """get_by_id() returns None when not found."""
        session = _make_session()
        session.get.return_value = None
        repo = SnapshotRepository(session)

        result = await repo.get_by_id(uuid.uuid4())

        assert result is None


class TestFindByIdentity:
    """SnapshotRepository.find_by_identity tests."""

    async def test_find_by_identity_found(self) -> None:
        """find_by_identity() returns matching snapshot."""
        session = _make_session()
        snap = _mock_snapshot()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = snap
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.find_by_identity(
            course_id=snap.course_id,
            node_id=snap.node_id,
            node_fingerprint=snap.node_fingerprint,
            mode=snap.mode,
        )

        assert result is snap

    async def test_find_by_identity_not_found(self) -> None:
        """find_by_identity() returns None when no match."""
        session = _make_session()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.find_by_identity(
            course_id=uuid.uuid4(),
            node_id=None,
            node_fingerprint="nonexistent",
            mode="free",
        )

        assert result is None

    async def test_find_by_identity_course_level(self) -> None:
        """find_by_identity() works with node_id=None (course-level)."""
        session = _make_session()
        snap = _mock_snapshot(node_id=None)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = snap
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.find_by_identity(
            course_id=snap.course_id,
            node_id=None,
            node_fingerprint=snap.node_fingerprint,
            mode=snap.mode,
        )

        assert result is snap
        assert result.node_id is None


class TestGetLatest:
    """SnapshotRepository.get_latest_for_node / get_latest_for_course tests."""

    async def test_get_latest_for_node(self) -> None:
        """get_latest_for_node() returns most recent snapshot for node."""
        session = _make_session()
        snap = _mock_snapshot(node_id=uuid.uuid4())
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = snap
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.get_latest_for_node(snap.course_id, snap.node_id)

        assert result is snap

    async def test_get_latest_for_node_empty(self) -> None:
        """get_latest_for_node() returns None when no snapshots exist."""
        session = _make_session()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.get_latest_for_node(uuid.uuid4(), uuid.uuid4())

        assert result is None

    async def test_get_latest_for_course(self) -> None:
        """get_latest_for_course() returns most recent course-level snapshot."""
        session = _make_session()
        snap = _mock_snapshot(node_id=None)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = snap
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.get_latest_for_course(snap.course_id)

        assert result is snap

    async def test_get_latest_for_course_empty(self) -> None:
        """get_latest_for_course() returns None when no snapshots exist."""
        session = _make_session()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.get_latest_for_course(uuid.uuid4())

        assert result is None


class TestListForCourse:
    """SnapshotRepository.list_for_course tests."""

    async def test_list_for_course_returns_all(self) -> None:
        """list_for_course() returns all snapshots for a course."""
        session = _make_session()
        course_id = uuid.uuid4()
        snaps = [
            _mock_snapshot(course_id=course_id),
            _mock_snapshot(course_id=course_id),
        ]
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = snaps
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalars_mock
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.list_for_course(course_id)

        assert len(result) == 2

    async def test_list_for_course_empty(self) -> None:
        """list_for_course() returns empty list when no snapshots."""
        session = _make_session()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalars_mock
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.list_for_course(uuid.uuid4())

        assert result == []
