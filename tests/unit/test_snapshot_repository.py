"""Tests for SnapshotRepository."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from course_supporter.storage.orm import StructureSnapshot
from course_supporter.storage.snapshot_repository import SnapshotRepository


def _mock_snapshot(
    *,
    snapshot_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    node_fingerprint: str = "abc123",
    mode: str = "free",
    structure: dict[str, object] | None = None,
    externalservicecall_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock StructureSnapshot."""
    snap = MagicMock(spec=StructureSnapshot)
    snap.id = snapshot_id or uuid.uuid4()
    snap.node_id = node_id or uuid.uuid4()
    snap.node_fingerprint = node_fingerprint
    snap.mode = mode
    snap.structure = structure or {"title": "Test"}
    snap.externalservicecall_id = externalservicecall_id
    snap.service_call = None
    return snap


def _make_session() -> AsyncMock:
    """Create an AsyncSession mock."""
    session = AsyncMock()
    session.add = MagicMock()
    return session


class TestCreate:
    """SnapshotRepository.create tests."""

    async def test_create_returns_snapshot(self) -> None:
        """create() returns a StructureSnapshot instance."""
        session = _make_session()
        repo = SnapshotRepository(session)

        result = await repo.create(
            node_id=uuid.uuid4(),
            node_fingerprint="abc123",
            mode="free",
            structure={"title": "Test"},
        )

        assert isinstance(result, StructureSnapshot)

    async def test_create_adds_to_session(self) -> None:
        """create() calls session.add()."""
        session = _make_session()
        repo = SnapshotRepository(session)

        await repo.create(
            node_id=uuid.uuid4(),
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
            node_id=uuid.uuid4(),
            node_fingerprint="abc123",
            mode="free",
            structure={"title": "Test"},
        )

        session.flush.assert_awaited_once()
        session.commit.assert_not_awaited()

    async def test_create_with_esc_fk(self) -> None:
        """create() passes externalservicecall_id to the ORM object."""
        session = _make_session()
        repo = SnapshotRepository(session)
        esc_id = uuid.uuid4()

        result = await repo.create(
            node_id=uuid.uuid4(),
            node_fingerprint="abc123",
            mode="guided",
            structure={"title": "Test"},
            externalservicecall_id=esc_id,
        )

        assert result.externalservicecall_id == esc_id

    async def test_create_with_node_id(self) -> None:
        """create() sets node_id for node-level snapshot."""
        session = _make_session()
        repo = SnapshotRepository(session)
        node_id = uuid.uuid4()

        result = await repo.create(
            node_id=node_id,
            node_fingerprint="abc123",
            mode="free",
            structure={"title": "Test"},
        )

        assert result.node_id == node_id


class TestGetById:
    """SnapshotRepository.get_by_id tests."""

    async def test_get_by_id_found(self) -> None:
        """get_by_id() returns snapshot when found."""
        session = _make_session()
        snap = _mock_snapshot()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = snap
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.get_by_id(snap.id)

        assert result is snap

    async def test_get_by_id_not_found(self) -> None:
        """get_by_id() returns None when not found."""
        session = _make_session()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        session.execute.return_value = exec_result
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
            node_id=uuid.uuid4(),
            node_fingerprint="nonexistent",
            mode="free",
        )

        assert result is None


class TestGetLatest:
    """SnapshotRepository.get_latest_for_node tests."""

    async def test_get_latest_for_node(self) -> None:
        """get_latest_for_node() returns most recent snapshot for node."""
        session = _make_session()
        snap = _mock_snapshot(node_id=uuid.uuid4())
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = snap
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.get_latest_for_node(snap.node_id)

        assert result is snap

    async def test_get_latest_for_node_empty(self) -> None:
        """get_latest_for_node() returns None when no snapshots exist."""
        session = _make_session()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.get_latest_for_node(uuid.uuid4())

        assert result is None


class TestCountForNode:
    """SnapshotRepository.count_for_node tests."""

    async def test_count_for_node(self) -> None:
        """count_for_node() returns integer count."""
        session = _make_session()
        exec_result = MagicMock()
        exec_result.scalar_one.return_value = 5
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.count_for_node(uuid.uuid4())

        assert result == 5

    async def test_count_for_node_empty(self) -> None:
        """count_for_node() returns 0 when no snapshots."""
        session = _make_session()
        exec_result = MagicMock()
        exec_result.scalar_one.return_value = 0
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.count_for_node(uuid.uuid4())

        assert result == 0


class TestListForNode:
    """SnapshotRepository.list_for_node tests."""

    async def test_list_for_node_returns_all(self) -> None:
        """list_for_node() returns all snapshots for a node."""
        session = _make_session()
        node_id = uuid.uuid4()
        snaps = [
            _mock_snapshot(node_id=node_id),
            _mock_snapshot(node_id=node_id),
        ]
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = snaps
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalars_mock
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.list_for_node(node_id)

        assert len(result) == 2

    async def test_list_for_node_empty(self) -> None:
        """list_for_node() returns empty list when no snapshots."""
        session = _make_session()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalars_mock
        session.execute.return_value = exec_result
        repo = SnapshotRepository(session)

        result = await repo.list_for_node(uuid.uuid4())

        assert result == []
