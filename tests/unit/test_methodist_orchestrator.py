"""Tests for Methodist orchestrator DAG construction."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.methodist_orchestrator import (
    MethodistPlan,
    _build_maps,
    _post_order,
    _pre_order,
    trigger_methodist,
)


def _mock_editable(
    *,
    editable_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    title: str = "Node",
) -> MagicMock:
    ed = MagicMock()
    ed.id = editable_id or uuid.uuid4()
    ed.parent_editable_id = parent_id
    ed.title = title
    return ed


class TestBuildMaps:
    def test_single_root(self) -> None:
        root = _mock_editable(title="Root")
        parent_map, children_map = _build_maps([root])
        assert parent_map[root.id] is None
        assert children_map[root.id] == []

    def test_parent_child(self) -> None:
        root = _mock_editable(title="Root")
        child = _mock_editable(parent_id=root.id, title="Child")
        parent_map, children_map = _build_maps([root, child])
        assert parent_map[child.id] == root.id
        assert children_map[root.id] == [child.id]


class TestTraversalOrder:
    def test_post_order(self) -> None:
        """Post-order: children before parents."""
        root = _mock_editable(title="Root")
        c1 = _mock_editable(parent_id=root.id, title="C1")
        c2 = _mock_editable(parent_id=root.id, title="C2")
        editables = [root, c1, c2]
        pm, cm = _build_maps(editables)
        order = _post_order(editables, pm, cm)
        assert order.index(c1.id) < order.index(root.id)
        assert order.index(c2.id) < order.index(root.id)

    def test_pre_order(self) -> None:
        """Pre-order: parents before children."""
        root = _mock_editable(title="Root")
        c1 = _mock_editable(parent_id=root.id, title="C1")
        c2 = _mock_editable(parent_id=root.id, title="C2")
        editables = [root, c1, c2]
        pm, cm = _build_maps(editables)
        order = _pre_order(editables, pm, cm)
        assert order.index(root.id) < order.index(c1.id)
        assert order.index(root.id) < order.index(c2.id)


class TestTriggerMethodist:
    async def test_empty_tree_raises(self) -> None:
        """Raises ValueError when no editable tree exists."""
        redis = AsyncMock()
        session = AsyncMock()
        mn_id = uuid.uuid4()

        with (
            patch(
                "course_supporter.storage.editable_repository.EditableRepository",
            ) as mock_repo_cls,
            pytest.raises(ValueError, match="No editable tree"),
        ):
            mock_repo_cls.return_value.get_tree = AsyncMock(
                return_value=[],
            )
            await trigger_methodist(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                materialnode_id=mn_id,
            )

    async def test_leaf_only_tree(self) -> None:
        """Single leaf node: 1 bottom-up job, 0 top-down jobs."""
        redis = AsyncMock()
        session = AsyncMock()
        mn_id = uuid.uuid4()
        leaf = _mock_editable(title="Leaf")

        mock_job = MagicMock()
        mock_job.id = uuid.uuid4()

        with (
            patch(
                "course_supporter.storage.editable_repository.EditableRepository",
            ) as mock_repo_cls,
            patch(
                "course_supporter.methodist_orchestrator._enqueue_methodist_step",
                new_callable=AsyncMock,
                return_value=mock_job,
            ),
        ):
            mock_repo_cls.return_value.get_tree = AsyncMock(
                return_value=[leaf],
            )
            plan = await trigger_methodist(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                materialnode_id=mn_id,
            )

        assert isinstance(plan, MethodistPlan)
        assert len(plan.bottom_up_jobs) == 1
        assert len(plan.top_down_jobs) == 0
        assert plan.estimated_llm_calls == 1

    async def test_parent_with_children(self) -> None:
        """Parent + 2 children: 3 bottom-up + 1 top-down."""
        redis = AsyncMock()
        session = AsyncMock()
        mn_id = uuid.uuid4()

        root = _mock_editable(title="Root")
        c1 = _mock_editable(parent_id=root.id, title="C1")
        c2 = _mock_editable(parent_id=root.id, title="C2")

        job_counter = 0

        async def _fake_enqueue(**kwargs: object) -> MagicMock:
            nonlocal job_counter
            job_counter += 1
            j = MagicMock()
            j.id = uuid.uuid4()
            return j

        with (
            patch(
                "course_supporter.storage.editable_repository.EditableRepository",
            ) as mock_repo_cls,
            patch(
                "course_supporter.methodist_orchestrator._enqueue_methodist_step",
                side_effect=_fake_enqueue,
            ),
        ):
            mock_repo_cls.return_value.get_tree = AsyncMock(
                return_value=[root, c1, c2],
            )
            plan = await trigger_methodist(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                materialnode_id=mn_id,
            )

        # 3 nodes bottom-up + 1 non-leaf top-down (root)
        assert len(plan.bottom_up_jobs) == 3
        assert len(plan.top_down_jobs) == 1
        assert plan.estimated_llm_calls == 4
