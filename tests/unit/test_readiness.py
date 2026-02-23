"""Tests for ReadinessService (subtree readiness check)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.readiness import ReadinessService, StaleMaterial
from course_supporter.storage.orm import MaterialEntry, MaterialNode, MaterialState


def _mock_entry(
    *,
    state: MaterialState = MaterialState.READY,
    filename: str | None = "file.pdf",
) -> MagicMock:
    """Create a mock MaterialEntry with the given derived state."""
    entry = MagicMock(spec=MaterialEntry)
    entry.id = uuid.uuid4()
    entry.filename = filename
    entry.state = state
    return entry


def _mock_node(
    *,
    node_id: uuid.UUID | None = None,
    course_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    title: str = "Node",
    materials: list[MagicMock] | None = None,
) -> MagicMock:
    """Create a mock MaterialNode."""
    node = MagicMock(spec=MaterialNode)
    node.id = node_id or uuid.uuid4()
    node.course_id = course_id or uuid.uuid4()
    node.parent_id = parent_id
    node.title = title
    node.materials = materials or []
    return node


def _make_session(
    root: MagicMock,
    all_nodes: list[MagicMock],
) -> AsyncMock:
    """Create an AsyncSession that returns root on get() and all_nodes on execute()."""
    session = AsyncMock()
    session.get.return_value = root

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = all_nodes
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars_mock
    session.execute.return_value = exec_result

    return session


class TestAllReady:
    """Subtree where every material is READY."""

    async def test_single_node_all_ready(self) -> None:
        """Single node with all READY materials returns ready=True."""
        course_id = uuid.uuid4()
        root = _mock_node(
            course_id=course_id,
            title="Root",
            materials=[_mock_entry(), _mock_entry()],
        )
        session = _make_session(root, [root])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        assert result.ready is True
        assert result.stale == []

    async def test_nested_all_ready(self) -> None:
        """Nested tree with all READY materials returns ready=True."""
        course_id = uuid.uuid4()
        root = _mock_node(
            course_id=course_id,
            title="Root",
            materials=[_mock_entry()],
        )
        child = _mock_node(
            course_id=course_id,
            parent_id=root.id,
            title="Child",
            materials=[_mock_entry()],
        )
        session = _make_session(root, [root, child])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        assert result.ready is True

    async def test_empty_tree_is_ready(self) -> None:
        """Node with no materials is considered ready."""
        course_id = uuid.uuid4()
        root = _mock_node(course_id=course_id, title="Empty", materials=[])
        session = _make_session(root, [root])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        assert result.ready is True
        assert result.stale == []


class TestStaleMaterials:
    """Subtrees with RAW or INTEGRITY_BROKEN materials."""

    async def test_raw_material_blocks(self) -> None:
        """RAW material makes subtree not ready."""
        course_id = uuid.uuid4()
        raw_entry = _mock_entry(state=MaterialState.RAW, filename="raw.mp4")
        root = _mock_node(
            course_id=course_id,
            title="Root",
            materials=[raw_entry],
        )
        session = _make_session(root, [root])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        assert result.ready is False
        assert len(result.stale) == 1
        assert result.stale[0].state == MaterialState.RAW
        assert result.stale[0].filename == "raw.mp4"

    async def test_integrity_broken_blocks(self) -> None:
        """INTEGRITY_BROKEN material makes subtree not ready."""
        course_id = uuid.uuid4()
        broken = _mock_entry(state=MaterialState.INTEGRITY_BROKEN, filename="old.pdf")
        root = _mock_node(
            course_id=course_id,
            title="Root",
            materials=[broken],
        )
        session = _make_session(root, [root])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        assert result.ready is False
        assert result.stale[0].state == MaterialState.INTEGRITY_BROKEN

    async def test_nested_stale_in_child(self) -> None:
        """Stale material in child node is detected."""
        course_id = uuid.uuid4()
        root = _mock_node(
            course_id=course_id,
            title="Root",
            materials=[_mock_entry()],
        )
        raw_entry = _mock_entry(state=MaterialState.RAW, filename="child.mp4")
        child = _mock_node(
            course_id=course_id,
            parent_id=root.id,
            title="Child",
            materials=[raw_entry],
        )
        session = _make_session(root, [root, child])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        assert result.ready is False
        assert len(result.stale) == 1
        assert result.stale[0].node_title == "Child"

    async def test_stale_in_grandchild(self) -> None:
        """Stale material in grandchild is detected via BFS."""
        course_id = uuid.uuid4()
        root = _mock_node(course_id=course_id, title="Root", materials=[])
        child = _mock_node(
            course_id=course_id,
            parent_id=root.id,
            title="Child",
            materials=[],
        )
        raw_entry = _mock_entry(state=MaterialState.RAW, filename="deep.pdf")
        grandchild = _mock_node(
            course_id=course_id,
            parent_id=child.id,
            title="Grandchild",
            materials=[raw_entry],
        )
        session = _make_session(root, [root, child, grandchild])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        assert result.ready is False
        assert len(result.stale) == 1
        assert result.stale[0].node_title == "Grandchild"

    async def test_multiple_stale_across_nodes(self) -> None:
        """Multiple stale materials across different nodes are all returned."""
        course_id = uuid.uuid4()
        raw1 = _mock_entry(state=MaterialState.RAW, filename="a.mp4")
        raw2 = _mock_entry(state=MaterialState.INTEGRITY_BROKEN, filename="b.pdf")
        root = _mock_node(course_id=course_id, title="Root", materials=[raw1])
        child = _mock_node(
            course_id=course_id,
            parent_id=root.id,
            title="Child",
            materials=[_mock_entry(), raw2],
        )
        session = _make_session(root, [root, child])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        assert result.ready is False
        assert len(result.stale) == 2


class TestNonBlockingStates:
    """PENDING and ERROR materials do not block readiness."""

    async def test_pending_does_not_block(self) -> None:
        """PENDING material is not considered stale."""
        course_id = uuid.uuid4()
        root = _mock_node(
            course_id=course_id,
            title="Root",
            materials=[_mock_entry(state=MaterialState.PENDING)],
        )
        session = _make_session(root, [root])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        assert result.ready is True

    async def test_error_does_not_block(self) -> None:
        """ERROR material is not considered stale."""
        course_id = uuid.uuid4()
        root = _mock_node(
            course_id=course_id,
            title="Root",
            materials=[_mock_entry(state=MaterialState.ERROR)],
        )
        session = _make_session(root, [root])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        assert result.ready is True


class TestMissingNode:
    """Edge case: node not found."""

    async def test_missing_node_raises(self) -> None:
        """Non-existent node raises ValueError."""
        session = AsyncMock()
        session.get.return_value = None
        svc = ReadinessService(session)

        with pytest.raises(ValueError, match="not found"):
            await svc.check_subtree(uuid.uuid4())


class TestStaleMaterialDataclass:
    """StaleMaterial field mapping."""

    async def test_stale_material_fields(self) -> None:
        """StaleMaterial captures entry_id, filename, state, node context."""
        course_id = uuid.uuid4()
        raw_entry = _mock_entry(state=MaterialState.RAW, filename="test.mp4")
        root = _mock_node(
            course_id=course_id,
            title="My Node",
            materials=[raw_entry],
        )
        session = _make_session(root, [root])
        svc = ReadinessService(session)

        result = await svc.check_subtree(root.id)

        stale = result.stale[0]
        assert isinstance(stale, StaleMaterial)
        assert stale.entry_id == raw_entry.id
        assert stale.filename == "test.mp4"
        assert stale.state == MaterialState.RAW
        assert stale.node_id == root.id
        assert stale.node_title == "My Node"


class TestSubtreeIsolation:
    """Only nodes in the subtree are checked, not siblings."""

    async def test_sibling_stale_not_included(self) -> None:
        """Stale material in a sibling node is not reported."""
        course_id = uuid.uuid4()
        root = _mock_node(course_id=course_id, title="Root", materials=[])
        target = _mock_node(
            course_id=course_id,
            parent_id=root.id,
            title="Target",
            materials=[_mock_entry()],
        )
        sibling = _mock_node(
            course_id=course_id,
            parent_id=root.id,
            title="Sibling",
            materials=[_mock_entry(state=MaterialState.RAW)],
        )
        # Check subtree of target only — sibling should be excluded
        session = _make_session(target, [root, target, sibling])
        svc = ReadinessService(session)

        result = await svc.check_subtree(target.id)

        assert result.ready is True
        assert result.stale == []
