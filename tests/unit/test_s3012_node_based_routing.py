"""Tests specific to S3-012: Node-based routing (Course → root MaterialNode).

Covers:
1. _find_root_id() — walks parent chain to find root node
2. _require_node_for_tenant() in generation.py — tenant isolation guard
3. Root node = Course — POST /nodes creates root, GET /nodes lists tenant roots
4. enqueue_generation effective_node_id logic
5. detect_conflict with root_node_id (recursive CTE parent map)
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.conflict_detection import detect_conflict
from course_supporter.storage.database import get_session

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)


# ── 1. _find_root_id ──


class TestFindRootId:
    """_find_root_id walks up the parent chain to the root."""

    async def test_root_returns_itself(self) -> None:
        """Root node (parent_id=None) returns its own id."""
        from course_supporter.api.routes.generation import _find_root_id

        root_id = uuid.uuid4()
        root = MagicMock()
        root.id = root_id
        root.parent_id = None

        session = AsyncMock()
        with patch(
            "course_supporter.api.routes.generation.MaterialNodeRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=root)
            result = await _find_root_id(session, root_id)

        assert result == root_id

    async def test_child_walks_to_root(self) -> None:
        """Child node walks up parent chain to root."""
        from course_supporter.api.routes.generation import _find_root_id

        root_id = uuid.uuid4()
        child_id = uuid.uuid4()

        root = MagicMock()
        root.id = root_id
        root.parent_id = None

        child = MagicMock()
        child.id = child_id
        child.parent_id = root_id

        lookup = {child_id: child, root_id: root}

        session = AsyncMock()
        with patch(
            "course_supporter.api.routes.generation.MaterialNodeRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(
                side_effect=lambda nid: lookup.get(nid)
            )
            result = await _find_root_id(session, child_id)

        assert result == root_id

    async def test_grandchild_walks_two_levels(self) -> None:
        """Grandchild walks 2 levels to root."""
        from course_supporter.api.routes.generation import _find_root_id

        root_id = uuid.uuid4()
        child_id = uuid.uuid4()
        grandchild_id = uuid.uuid4()

        root = MagicMock(id=root_id, parent_id=None)
        child = MagicMock(id=child_id, parent_id=root_id)
        grandchild = MagicMock(id=grandchild_id, parent_id=child_id)

        lookup = {grandchild_id: grandchild, child_id: child, root_id: root}

        session = AsyncMock()
        with patch(
            "course_supporter.api.routes.generation.MaterialNodeRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(
                side_effect=lambda nid: lookup.get(nid)
            )
            result = await _find_root_id(session, grandchild_id)

        assert result == root_id

    async def test_missing_node_returns_fallback(self) -> None:
        """If node is not found mid-chain, returns original id as fallback."""
        from course_supporter.api.routes.generation import _find_root_id

        node_id = uuid.uuid4()

        session = AsyncMock()
        with patch(
            "course_supporter.api.routes.generation.MaterialNodeRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(
                return_value=None,
            )
            result = await _find_root_id(session, node_id)

        assert result == node_id


# ── 2. _require_node_for_tenant (generation.py) ──


class TestRequireNodeForTenantGeneration:
    """_require_node_for_tenant rejects wrong tenant or missing node."""

    async def test_returns_node_on_match(self) -> None:
        """Returns node when tenant_id matches."""
        from course_supporter.api.routes.generation import (
            _require_node_for_tenant,
        )

        tid = uuid.uuid4()
        nid = uuid.uuid4()
        node = MagicMock(id=nid, tenant_id=tid)

        session = AsyncMock()
        with patch(
            "course_supporter.api.routes.generation.MaterialNodeRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(
                return_value=node,
            )
            result = await _require_node_for_tenant(session, tid, nid)

        assert result is node

    async def test_raises_404_when_missing(self) -> None:
        """Raises HTTPException 404 when node not found."""
        from fastapi import HTTPException

        from course_supporter.api.routes.generation import (
            _require_node_for_tenant,
        )

        session = AsyncMock()
        with (
            patch(
                "course_supporter.api.routes.generation.MaterialNodeRepository"
            ) as repo_cls,
            pytest.raises(HTTPException) as exc_info,
        ):
            repo_cls.return_value.get_by_id = AsyncMock(
                return_value=None,
            )
            await _require_node_for_tenant(
                session,
                uuid.uuid4(),
                uuid.uuid4(),
            )

        assert exc_info.value.status_code == 404

    async def test_raises_404_when_wrong_tenant(self) -> None:
        """Raises HTTPException 404 when node belongs to another tenant."""
        from fastapi import HTTPException

        from course_supporter.api.routes.generation import (
            _require_node_for_tenant,
        )

        node = MagicMock(id=uuid.uuid4(), tenant_id=uuid.uuid4())

        session = AsyncMock()
        with (
            patch(
                "course_supporter.api.routes.generation.MaterialNodeRepository"
            ) as repo_cls,
            pytest.raises(HTTPException) as exc_info,
        ):
            repo_cls.return_value.get_by_id = AsyncMock(
                return_value=node,
            )
            different_tenant = uuid.uuid4()
            await _require_node_for_tenant(
                session,
                different_tenant,
                node.id,
            )

        assert exc_info.value.status_code == 404


# ── 3. Root node = Course (POST /nodes, GET /nodes) ──


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_arq_redis] = lambda: AsyncMock()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


def _mock_root_node(
    *,
    node_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    title: str = "My Course",
) -> MagicMock:
    from datetime import UTC, datetime

    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.tenant_id = tenant_id or STUB_TENANT.tenant_id
    node.parent_id = None
    node.title = title
    node.description = None
    node.order = 0
    node.node_fingerprint = None
    node.learning_goal = None
    node.expected_knowledge = None
    node.expected_skills = None
    node.children = []
    node.created_at = datetime.now(UTC)
    node.updated_at = datetime.now(UTC)
    return node


class TestRootNodeAsCourse:
    """POST /nodes creates a root node; GET /nodes lists only tenant roots."""

    async def test_post_nodes_creates_root(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /api/v1/nodes creates a root node (parent_id=None)."""
        from course_supporter.storage.material_node_repository import (
            MaterialNodeRepository,
        )

        root = _mock_root_node(title="Python Basics")
        with patch.object(
            MaterialNodeRepository,
            "create",
            return_value=root,
        ):
            resp = await client.post(
                "/api/v1/nodes",
                json={"title": "Python Basics"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["parent_id"] is None
        assert data["title"] == "Python Basics"
        assert data["tenant_id"] == str(STUB_TENANT.tenant_id)

    async def test_get_nodes_returns_only_tenant_roots(
        self,
        client: AsyncClient,
    ) -> None:
        """GET /api/v1/nodes returns only root nodes for the tenant."""
        from course_supporter.storage.material_node_repository import (
            MaterialNodeRepository,
        )

        r1 = _mock_root_node(title="Course A")
        r2 = _mock_root_node(title="Course B")
        with (
            patch.object(
                MaterialNodeRepository,
                "list_roots",
                return_value=[r1, r2],
            ),
            patch.object(
                MaterialNodeRepository,
                "count_roots",
                return_value=2,
            ),
        ):
            resp = await client.get("/api/v1/nodes")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert all(item["parent_id"] is None for item in data["items"])

    async def test_other_tenant_roots_not_visible(
        self,
        client: AsyncClient,
    ) -> None:
        """GET /api/v1/nodes returns empty for tenant with no roots."""
        from course_supporter.storage.material_node_repository import (
            MaterialNodeRepository,
        )

        with (
            patch.object(
                MaterialNodeRepository,
                "list_roots",
                return_value=[],
            ),
            patch.object(
                MaterialNodeRepository,
                "count_roots",
                return_value=0,
            ),
        ):
            resp = await client.get("/api/v1/nodes")

        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["items"] == []


# ── 4. enqueue_generation effective_node_id ──


class TestEnqueueGenerationEffectiveNodeId:
    """enqueue_generation stores effective_node_id = target or root."""

    async def test_target_node_becomes_effective(self) -> None:
        """When target_node_id given, Job.node_id = target_node_id."""
        from course_supporter.enqueue import enqueue_generation

        session = AsyncMock()
        session.flush = AsyncMock()
        redis = AsyncMock()
        arq_job = MagicMock()
        arq_job.job_id = "arq:test:1"
        redis.enqueue_job = AsyncMock(return_value=arq_job)

        mock_job = MagicMock()
        mock_job.id = uuid.uuid4()

        root_id = uuid.uuid4()
        target_id = uuid.uuid4()

        with patch(
            "course_supporter.enqueue.JobRepository",
        ) as repo_cls:
            repo_cls.return_value.create = AsyncMock(
                return_value=mock_job,
            )
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_generation(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                root_node_id=root_id,
                target_node_id=target_id,
            )

        create_kw = repo_cls.return_value.create.call_args.kwargs
        assert create_kw["node_id"] == target_id

    async def test_none_target_uses_root(self) -> None:
        """When target_node_id=None, Job.node_id = root_node_id."""
        from course_supporter.enqueue import enqueue_generation

        session = AsyncMock()
        session.flush = AsyncMock()
        redis = AsyncMock()
        arq_job = MagicMock()
        arq_job.job_id = "arq:test:2"
        redis.enqueue_job = AsyncMock(return_value=arq_job)

        mock_job = MagicMock()
        mock_job.id = uuid.uuid4()

        root_id = uuid.uuid4()

        with patch(
            "course_supporter.enqueue.JobRepository",
        ) as repo_cls:
            repo_cls.return_value.create = AsyncMock(
                return_value=mock_job,
            )
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_generation(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                root_node_id=root_id,
                target_node_id=None,
            )

        create_kw = repo_cls.return_value.create.call_args.kwargs
        assert create_kw["node_id"] == root_id

    async def test_input_params_store_both_ids(self) -> None:
        """input_params stores root_node_id and target_node_id separately."""
        from course_supporter.enqueue import enqueue_generation

        session = AsyncMock()
        session.flush = AsyncMock()
        redis = AsyncMock()
        arq_job = MagicMock()
        arq_job.job_id = "arq:test:3"
        redis.enqueue_job = AsyncMock(return_value=arq_job)

        mock_job = MagicMock()
        mock_job.id = uuid.uuid4()

        root_id = uuid.uuid4()
        target_id = uuid.uuid4()

        with patch(
            "course_supporter.enqueue.JobRepository",
        ) as repo_cls:
            repo_cls.return_value.create = AsyncMock(
                return_value=mock_job,
            )
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_generation(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                root_node_id=root_id,
                target_node_id=target_id,
            )

        params = repo_cls.return_value.create.call_args.kwargs["input_params"]
        assert params["root_node_id"] == str(root_id)
        assert params["target_node_id"] == str(target_id)


# ── 5. detect_conflict with root_node_id (parent map) ──


def _mock_session_with_tree(
    nodes: dict[uuid.UUID, uuid.UUID | None],
) -> AsyncMock:
    """Create a mock session that returns parent_map rows from execute()."""
    rows = []
    for nid, pid in nodes.items():
        row = MagicMock()
        row.id = nid
        row.parent_id = pid
        rows.append(row)

    exec_result = MagicMock()
    exec_result.all.return_value = rows
    session = AsyncMock()
    session.execute.return_value = exec_result
    return session


def _mock_job(
    node_id: uuid.UUID | None = None,
) -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.node_id = node_id
    return job


class TestDetectConflictWithRootNodeId:
    """detect_conflict uses root_node_id to load tree via CTE."""

    async def test_uses_root_node_id_for_parent_map(self) -> None:
        """parent_map query uses root_node_id, not some course_id."""
        root_id = uuid.uuid4()
        child_id = uuid.uuid4()
        nodes = {root_id: None, child_id: root_id}
        session = _mock_session_with_tree(nodes)

        await detect_conflict(
            session,
            root_node_id=root_id,
            target_node_id=child_id,
            active_jobs=[],
        )

        # The execute was called with root_node_id-based CTE
        session.execute.assert_called_once()

    async def test_root_vs_root_conflicts(self) -> None:
        """Two whole-tree scopes (None) on same root conflict."""
        root_id = uuid.uuid4()
        session = _mock_session_with_tree({root_id: None})
        job = _mock_job(node_id=None)

        result = await detect_conflict(
            session,
            root_node_id=root_id,
            target_node_id=None,
            active_jobs=[job],
        )

        assert result is not None
        assert "entire tree" in result.reason

    async def test_child_under_active_root_conflicts(self) -> None:
        """Active job on whole tree conflicts with child target."""
        root_id = uuid.uuid4()
        child_id = uuid.uuid4()
        session = _mock_session_with_tree(
            {root_id: None, child_id: root_id},
        )
        job = _mock_job(node_id=None)  # whole tree

        result = await detect_conflict(
            session,
            root_node_id=root_id,
            target_node_id=child_id,
            active_jobs=[job],
        )

        assert result is not None
        assert "entire tree" in result.reason

    async def test_independent_siblings_no_conflict(self) -> None:
        """Two siblings under same root do not conflict."""
        root_id = uuid.uuid4()
        sib_a = uuid.uuid4()
        sib_b = uuid.uuid4()
        nodes = {root_id: None, sib_a: root_id, sib_b: root_id}
        session = _mock_session_with_tree(nodes)
        job = _mock_job(node_id=sib_a)

        result = await detect_conflict(
            session,
            root_node_id=root_id,
            target_node_id=sib_b,
            active_jobs=[job],
        )

        assert result is None

    async def test_ancestor_descendant_conflict(self) -> None:
        """Active job on parent conflicts with grandchild target."""
        root_id = uuid.uuid4()
        parent_id = uuid.uuid4()
        grandchild_id = uuid.uuid4()
        nodes = {
            root_id: None,
            parent_id: root_id,
            grandchild_id: parent_id,
        }
        session = _mock_session_with_tree(nodes)
        job = _mock_job(node_id=parent_id)

        result = await detect_conflict(
            session,
            root_node_id=root_id,
            target_node_id=grandchild_id,
            active_jobs=[job],
        )

        assert result is not None
        assert "nested inside active job" in result.reason
