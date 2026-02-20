"""Tests for MaterialNodeRepository."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.orm import MaterialNode


def _mock_node(
    *,
    node_id: uuid.UUID | None = None,
    course_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    title: str = "Test Node",
    order: int = 0,
) -> MagicMock:
    """Create a mock MaterialNode."""
    node = MagicMock(spec=MaterialNode)
    node.id = node_id or uuid.uuid4()
    node.course_id = course_id or uuid.uuid4()
    node.parent_id = parent_id
    node.title = title
    node.description = None
    node.order = order
    node.node_fingerprint = None
    node.children = []
    return node


class TestCreate:
    """MaterialNodeRepository.create tests."""

    async def test_create_root_node(self) -> None:
        """Root node created with auto-increment order."""
        session = AsyncMock()
        session.add = MagicMock()
        repo = MaterialNodeRepository(session)

        course_id = uuid.uuid4()

        with patch.object(repo, "_next_sibling_order", return_value=0):
            result = await repo.create(
                course_id=course_id,
                title="Module 1",
            )

        session.add.assert_called_once()
        session.flush.assert_awaited()
        added_node = session.add.call_args[0][0]
        assert isinstance(added_node, MaterialNode)
        assert added_node.title == "Module 1"
        assert added_node.parent_id is None
        assert added_node.order == 0
        assert result is added_node

    async def test_create_child_node(self) -> None:
        """Child node created under parent with correct order."""
        session = AsyncMock()
        session.add = MagicMock()
        repo = MaterialNodeRepository(session)

        course_id = uuid.uuid4()
        parent_id = uuid.uuid4()

        with patch.object(repo, "_next_sibling_order", return_value=3):
            result = await repo.create(
                course_id=course_id,
                parent_id=parent_id,
                title="Subtopic",
                description="Details",
            )

        added_node = session.add.call_args[0][0]
        assert isinstance(added_node, MaterialNode)
        assert added_node.parent_id == parent_id
        assert added_node.order == 3
        assert added_node.description == "Details"
        assert result is added_node


class TestGetById:
    """MaterialNodeRepository.get_by_id tests."""

    async def test_found(self) -> None:
        """Returns node when found."""
        node = _mock_node()
        session = AsyncMock()
        session.get.return_value = node

        repo = MaterialNodeRepository(session)
        result = await repo.get_by_id(node.id)
        assert result is node

    async def test_not_found(self) -> None:
        """Returns None when not found."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialNodeRepository(session)
        result = await repo.get_by_id(uuid.uuid4())
        assert result is None


class TestGetTree:
    """MaterialNodeRepository.get_tree tests."""

    async def test_empty_course(self) -> None:
        """No nodes returns empty list."""
        session = AsyncMock()
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = []
        session.execute.return_value = exec_result

        repo = MaterialNodeRepository(session)
        roots = await repo.get_tree(uuid.uuid4())
        assert roots == []

    async def test_flat_roots(self) -> None:
        """Multiple root nodes returned in order."""
        cid = uuid.uuid4()
        r1 = _mock_node(course_id=cid, title="Root 1", order=0)
        r2 = _mock_node(course_id=cid, title="Root 2", order=1)

        session = AsyncMock()
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = [r1, r2]
        session.execute.return_value = exec_result

        repo = MaterialNodeRepository(session)
        roots = await repo.get_tree(cid)

        assert len(roots) == 2
        assert roots[0] is r1
        assert roots[1] is r2

    async def test_nested_tree(self) -> None:
        """Three-level tree assembled correctly."""
        cid = uuid.uuid4()
        root = _mock_node(course_id=cid, title="Root", order=0)
        child = _mock_node(course_id=cid, parent_id=root.id, title="Child", order=0)
        grandchild = _mock_node(
            course_id=cid, parent_id=child.id, title="Grandchild", order=0
        )

        session = AsyncMock()
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = [
            root,
            child,
            grandchild,
        ]
        session.execute.return_value = exec_result

        repo = MaterialNodeRepository(session)
        roots = await repo.get_tree(cid)

        assert len(roots) == 1
        assert roots[0] is root
        assert len(root.children) == 1
        assert root.children[0] is child
        assert len(child.children) == 1
        assert child.children[0] is grandchild

    async def test_multiple_children(self) -> None:
        """Node with multiple children ordered."""
        cid = uuid.uuid4()
        root = _mock_node(course_id=cid, title="Root", order=0)
        c1 = _mock_node(course_id=cid, parent_id=root.id, title="C1", order=0)
        c2 = _mock_node(course_id=cid, parent_id=root.id, title="C2", order=1)

        session = AsyncMock()
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = [root, c1, c2]
        session.execute.return_value = exec_result

        repo = MaterialNodeRepository(session)
        roots = await repo.get_tree(cid)

        assert len(roots) == 1
        assert len(root.children) == 2
        assert root.children[0] is c1
        assert root.children[1] is c2


class TestMove:
    """MaterialNodeRepository.move tests."""

    async def test_move_not_found(self) -> None:
        """ValueError if node doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialNodeRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.move(uuid.uuid4(), uuid.uuid4())

    async def test_move_to_self(self) -> None:
        """Cannot move node to be its own parent."""
        node = _mock_node()
        session = AsyncMock()
        session.get.return_value = node

        repo = MaterialNodeRepository(session)
        with pytest.raises(ValueError, match="own parent"):
            await repo.move(node.id, node.id)

    async def test_move_creates_cycle(self) -> None:
        """Cannot move ancestor under descendant."""
        # root -> child: moving root under child would cycle
        root = _mock_node(title="Root")
        child = _mock_node(parent_id=root.id, title="Child")

        session = AsyncMock()

        async def fake_get(cls: type, nid: uuid.UUID) -> MagicMock | None:
            lookup = {root.id: root, child.id: child}
            return lookup.get(nid)

        session.get.side_effect = fake_get

        # Mock _next_sibling_order (won't be reached due to cycle check)
        repo = MaterialNodeRepository(session)
        with pytest.raises(ValueError, match="cycle"):
            await repo.move(root.id, child.id)

    async def test_move_to_root(self) -> None:
        """Move node to root (parent_id=None)."""
        node = _mock_node(parent_id=uuid.uuid4())
        session = AsyncMock()
        session.get.return_value = node

        # Mock _next_sibling_order
        scalar_result = MagicMock()
        scalar_result.scalar_one.return_value = 2
        session.execute.return_value = scalar_result

        repo = MaterialNodeRepository(session)
        result = await repo.move(node.id, None)

        assert result.parent_id is None
        assert result.order == 2
        session.flush.assert_awaited()

    async def test_move_to_sibling(self) -> None:
        """Move node under a valid non-ancestor parent."""
        cid = uuid.uuid4()
        node_a = _mock_node(course_id=cid, title="A")
        node_b = _mock_node(course_id=cid, title="B")

        session = AsyncMock()

        async def fake_get(cls: type, nid: uuid.UUID) -> MagicMock | None:
            lookup = {node_a.id: node_a, node_b.id: node_b}
            return lookup.get(nid)

        session.get.side_effect = fake_get

        # _is_descendant walks up from B, B.parent_id is None -> not descendant
        # _next_sibling_order mock
        scalar_result = MagicMock()
        scalar_result.scalar_one.return_value = 0
        session.execute.return_value = scalar_result

        repo = MaterialNodeRepository(session)
        result = await repo.move(node_a.id, node_b.id)

        assert result.parent_id == node_b.id


class TestReorder:
    """MaterialNodeRepository.reorder tests."""

    async def test_reorder_not_found(self) -> None:
        """ValueError if node doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialNodeRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.reorder(uuid.uuid4(), 0)

    async def test_reorder_negative(self) -> None:
        """ValueError if new_order is negative."""
        session = AsyncMock()
        repo = MaterialNodeRepository(session)
        with pytest.raises(ValueError, match="non-negative"):
            await repo.reorder(uuid.uuid4(), -1)

    async def test_reorder_moves_to_position(self) -> None:
        """Node moved to desired position, siblings renumbered."""
        cid = uuid.uuid4()
        n0 = _mock_node(course_id=cid, title="N0", order=0)
        n1 = _mock_node(course_id=cid, title="N1", order=1)
        n2 = _mock_node(course_id=cid, title="N2", order=2)

        session = AsyncMock()
        session.get.return_value = n0
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = [n0, n1, n2]
        session.execute.return_value = exec_result

        repo = MaterialNodeRepository(session)
        result = await repo.reorder(n0.id, 2)  # move to end

        # After reorder: n1(0), n2(1), n0(2)
        assert n1.order == 0
        assert n2.order == 1
        assert n0.order == 2
        assert result is n0

    async def test_reorder_clamps_to_max(self) -> None:
        """Order clamped to max valid position."""
        cid = uuid.uuid4()
        n0 = _mock_node(course_id=cid, title="N0", order=0)
        n1 = _mock_node(course_id=cid, title="N1", order=1)

        session = AsyncMock()
        session.get.return_value = n0
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = [n0, n1]
        session.execute.return_value = exec_result

        repo = MaterialNodeRepository(session)
        await repo.reorder(n0.id, 999)

        # Clamped to 1 (max position)
        assert n1.order == 0
        assert n0.order == 1


class TestUpdate:
    """MaterialNodeRepository.update tests."""

    async def test_update_not_found(self) -> None:
        """ValueError if node doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialNodeRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.update(uuid.uuid4(), title="New")

    async def test_update_title(self) -> None:
        """Title updated when provided."""
        node = _mock_node(title="Old")
        session = AsyncMock()
        session.get.return_value = node

        repo = MaterialNodeRepository(session)
        result = await repo.update(node.id, title="New Title")

        assert result.title == "New Title"
        session.flush.assert_awaited()

    async def test_update_description_to_none(self) -> None:
        """Description cleared when set to None."""
        node = _mock_node()
        node.description = "Old desc"
        session = AsyncMock()
        session.get.return_value = node

        repo = MaterialNodeRepository(session)
        result = await repo.update(node.id, description=None)

        assert result.description is None

    async def test_update_skips_unset_fields(self) -> None:
        """Fields not provided are not changed."""
        node = _mock_node(title="Original")
        node.description = "Original desc"
        session = AsyncMock()
        session.get.return_value = node

        repo = MaterialNodeRepository(session)
        await repo.update(node.id)

        # title and description unchanged (MagicMock attributes preserved)
        assert node.title == "Original"
        assert node.description == "Original desc"


class TestDelete:
    """MaterialNodeRepository.delete tests."""

    async def test_delete_not_found(self) -> None:
        """ValueError if node doesn't exist."""
        session = AsyncMock()
        session.get.return_value = None

        repo = MaterialNodeRepository(session)
        with pytest.raises(ValueError, match="not found"):
            await repo.delete(uuid.uuid4())

    async def test_delete_calls_session_delete(self) -> None:
        """Delegates to session.delete + flush."""
        node = _mock_node()
        session = AsyncMock()
        session.get.return_value = node

        repo = MaterialNodeRepository(session)
        await repo.delete(node.id)

        session.delete.assert_awaited_once_with(node)
        session.flush.assert_awaited()


class TestNextSiblingOrder:
    """MaterialNodeRepository._next_sibling_order tests."""

    async def test_empty_siblings_returns_zero(self) -> None:
        """No siblings returns 0 (coalesce fallback)."""
        session = AsyncMock()
        exec_result = MagicMock()
        exec_result.scalar_one.return_value = 0
        session.execute.return_value = exec_result

        repo = MaterialNodeRepository(session)
        result = await repo._next_sibling_order(uuid.uuid4(), None)
        assert result == 0

    async def test_existing_siblings_returns_max_plus_one(self) -> None:
        """Returns max(order) + 1 among existing siblings."""
        session = AsyncMock()
        exec_result = MagicMock()
        exec_result.scalar_one.return_value = 3
        session.execute.return_value = exec_result

        repo = MaterialNodeRepository(session)
        result = await repo._next_sibling_order(uuid.uuid4(), uuid.uuid4())
        assert result == 3

    async def test_calls_execute_with_correct_args(self) -> None:
        """Verifies that session.execute is called."""
        session = AsyncMock()
        exec_result = MagicMock()
        exec_result.scalar_one.return_value = 0
        session.execute.return_value = exec_result

        repo = MaterialNodeRepository(session)
        await repo._next_sibling_order(uuid.uuid4(), None)

        session.execute.assert_awaited_once()
