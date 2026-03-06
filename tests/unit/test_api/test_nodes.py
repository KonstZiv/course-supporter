"""Tests for material tree node API endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.material_node_repository import MaterialNodeRepository

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)


def _mock_node(
    *,
    node_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    parent_materialnode_id: uuid.UUID | None = None,
    title: str = "Test Node",
    description: str | None = None,
    order: int = 0,
    children: list[object] | None = None,
) -> MagicMock:
    """Create a mock MaterialNode with ORM-compatible attributes."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.tenant_id = tenant_id or STUB_TENANT.tenant_id
    node.parent_materialnode_id = parent_materialnode_id
    node.title = title
    node.description = description
    node.order = order
    node.node_fingerprint = None
    node.learning_goal = None
    node.expected_knowledge = None
    node.expected_skills = None
    node.children = children or []
    node.created_at = datetime.now(UTC)
    node.updated_at = datetime.now(UTC)
    return node


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.extract_key = MagicMock(return_value=None)
    return s3


@pytest.fixture()
async def client(mock_session: AsyncMock, mock_s3: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestCreateRootNode:
    """POST /api/v1/nodes"""

    async def test_returns_201(self, client: AsyncClient) -> None:
        """Successful root node creation returns 201."""
        node = _mock_node()
        with patch.object(MaterialNodeRepository, "create", return_value=node):
            resp = await client.post("/api/v1/nodes", json={"title": "Module 1"})
        assert resp.status_code == 201

    async def test_returns_node_fields(self, client: AsyncClient) -> None:
        """Response contains all expected node fields."""
        node = _mock_node(title="Module 1")
        with patch.object(MaterialNodeRepository, "create", return_value=node):
            resp = await client.post("/api/v1/nodes", json={"title": "Module 1"})
        data = resp.json()
        assert data["id"] == str(node.id)
        assert data["title"] == "Module 1"
        assert data["parent_materialnode_id"] is None
        assert data["tenant_id"] == str(STUB_TENANT.tenant_id)
        assert "created_at" in data
        assert "updated_at" in data

    async def test_with_description(self, client: AsyncClient) -> None:
        """Root node accepts optional description."""
        node = _mock_node(description="Details")
        with patch.object(MaterialNodeRepository, "create", return_value=node):
            resp = await client.post(
                "/api/v1/nodes", json={"title": "Mod", "description": "Details"}
            )
        assert resp.status_code == 201
        assert resp.json()["description"] == "Details"

    async def test_empty_title_returns_422(self, client: AsyncClient) -> None:
        """Empty title is rejected with 422."""
        resp = await client.post("/api/v1/nodes", json={"title": ""})
        assert resp.status_code == 422

    async def test_missing_title_returns_422(self, client: AsyncClient) -> None:
        """Missing title is rejected with 422."""
        resp = await client.post("/api/v1/nodes", json={})
        assert resp.status_code == 422


class TestCreateChildNode:
    """POST /api/v1/nodes/{nid}/children"""

    async def test_returns_201(self, client: AsyncClient) -> None:
        """Successful child creation returns 201."""
        parent = _mock_node(title="Parent")
        child = _mock_node(parent_materialnode_id=parent.id, title="Child")
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=parent),
            patch.object(MaterialNodeRepository, "create", return_value=child),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{parent.id}/children", json={"title": "Child"}
            )
        assert resp.status_code == 201
        assert resp.json()["parent_materialnode_id"] == str(parent.id)

    async def test_parent_not_found_returns_404(self, client: AsyncClient) -> None:
        """Non-existent parent returns 404."""
        with patch.object(MaterialNodeRepository, "get_by_id", return_value=None):
            resp = await client.post(
                f"/api/v1/nodes/{uuid.uuid4()}/children", json={"title": "Child"}
            )
        assert resp.status_code == 404
        assert "Node not found" in resp.json()["detail"]

    async def test_parent_wrong_tenant_returns_404(self, client: AsyncClient) -> None:
        """Parent belonging to a different tenant returns 404."""
        parent = _mock_node(tenant_id=uuid.uuid4(), title="Wrong tenant parent")
        with patch.object(MaterialNodeRepository, "get_by_id", return_value=parent):
            resp = await client.post(
                f"/api/v1/nodes/{parent.id}/children", json={"title": "Child"}
            )
        assert resp.status_code == 404


class TestGetTree:
    """GET /api/v1/nodes/{nid}/tree"""

    async def test_returns_empty_list(self, client: AsyncClient) -> None:
        """Node with no children returns empty list."""
        root = _mock_node(title="Root")
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=root),
            patch.object(MaterialNodeRepository, "get_subtree", return_value=[]),
        ):
            resp = await client.get(f"/api/v1/nodes/{root.id}/tree")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_nested_tree(self, client: AsyncClient) -> None:
        """Tree with parent-child structure returned nested."""
        root = _mock_node(title="Root")
        child = _mock_node(title="Child")
        tree_root = _mock_node(title="Root", children=[child])
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=root),
            patch.object(
                MaterialNodeRepository, "get_subtree", return_value=[tree_root]
            ),
        ):
            resp = await client.get(f"/api/v1/nodes/{root.id}/tree")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Root"
        assert len(data[0]["children"]) == 1
        assert data[0]["children"][0]["title"] == "Child"

    async def test_node_not_found_returns_404(self, client: AsyncClient) -> None:
        """Non-existent node returns 404."""
        with patch.object(MaterialNodeRepository, "get_by_id", return_value=None):
            resp = await client.get(f"/api/v1/nodes/{uuid.uuid4()}/tree")
        assert resp.status_code == 404


class TestGetNode:
    """GET /api/v1/nodes/{nid}"""

    async def test_returns_node(self, client: AsyncClient) -> None:
        """Existing node returned with correct fields."""
        node = _mock_node(title="Node 1", order=2)
        with patch.object(MaterialNodeRepository, "get_by_id", return_value=node):
            resp = await client.get(f"/api/v1/nodes/{node.id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Node 1"
        assert resp.json()["order"] == 2

    async def test_not_found_returns_404(self, client: AsyncClient) -> None:
        """Non-existent node returns 404."""
        with patch.object(MaterialNodeRepository, "get_by_id", return_value=None):
            resp = await client.get(f"/api/v1/nodes/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestUpdateNode:
    """PATCH /api/v1/nodes/{nid}"""

    async def test_update_title(self, client: AsyncClient) -> None:
        """Title updated when provided."""
        node = _mock_node(title="Old")
        updated = _mock_node(title="New Title")
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "update", return_value=updated),
        ):
            resp = await client.patch(
                f"/api/v1/nodes/{node.id}", json={"title": "New Title"}
            )
        assert resp.status_code == 200
        assert resp.json()["title"] == "New Title"

    async def test_clear_description(self, client: AsyncClient) -> None:
        """Description cleared when set to null."""
        node = _mock_node(description="Old desc")
        updated = _mock_node(description=None)
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "update", return_value=updated),
        ):
            resp = await client.patch(
                f"/api/v1/nodes/{node.id}", json={"description": None}
            )
        assert resp.status_code == 200
        assert resp.json()["description"] is None

    async def test_empty_body_is_valid(self, client: AsyncClient) -> None:
        """Empty body (no fields to update) is accepted."""
        node = _mock_node()
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "update", return_value=node),
        ):
            resp = await client.patch(f"/api/v1/nodes/{node.id}", json={})
        assert resp.status_code == 200

    async def test_not_found_returns_404(self, client: AsyncClient) -> None:
        """Non-existent node returns 404."""
        with patch.object(MaterialNodeRepository, "get_by_id", return_value=None):
            resp = await client.patch(
                f"/api/v1/nodes/{uuid.uuid4()}", json={"title": "New"}
            )
        assert resp.status_code == 404


class TestMoveNode:
    """POST /api/v1/nodes/{nid}/move"""

    async def test_move_to_new_parent(self, client: AsyncClient) -> None:
        """Node moved to a new parent."""
        node = _mock_node(title="Movable")
        target = _mock_node(title="Target")
        moved = _mock_node(parent_materialnode_id=target.id)

        def fake_get(nid: uuid.UUID) -> MagicMock | None:
            lookup = {node.id: node, target.id: target}
            return lookup.get(nid)

        with (
            patch.object(MaterialNodeRepository, "get_by_id", side_effect=fake_get),
            patch.object(MaterialNodeRepository, "move", return_value=moved),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node.id}/move",
                json={"parent_materialnode_id": str(target.id)},
            )
        assert resp.status_code == 200
        assert resp.json()["parent_materialnode_id"] == str(target.id)

    async def test_move_to_root(self, client: AsyncClient) -> None:
        """Node moved to root (parent_materialnode_id=null)."""
        node = _mock_node(parent_materialnode_id=uuid.uuid4())
        moved = _mock_node(parent_materialnode_id=None)
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "move", return_value=moved),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node.id}/move", json={"parent_materialnode_id": None}
            )
        assert resp.status_code == 200
        assert resp.json()["parent_materialnode_id"] is None

    async def test_cycle_returns_422(self, client: AsyncClient) -> None:
        """Cycle detection error returns 422."""
        node = _mock_node()
        target = _mock_node()

        def fake_get(nid: uuid.UUID) -> MagicMock | None:
            lookup = {node.id: node, target.id: target}
            return lookup.get(nid)

        with (
            patch.object(MaterialNodeRepository, "get_by_id", side_effect=fake_get),
            patch.object(
                MaterialNodeRepository,
                "move",
                side_effect=ValueError("would create a cycle"),
            ),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node.id}/move",
                json={"parent_materialnode_id": str(target.id)},
            )
        assert resp.status_code == 422
        assert "cycle" in resp.json()["detail"]

    async def test_target_wrong_tenant_returns_404(self, client: AsyncClient) -> None:
        """Target parent in different tenant returns 404."""
        node = _mock_node()
        target = _mock_node(tenant_id=uuid.uuid4())

        def fake_get(nid: uuid.UUID) -> MagicMock | None:
            lookup = {node.id: node, target.id: target}
            return lookup.get(nid)

        with patch.object(MaterialNodeRepository, "get_by_id", side_effect=fake_get):
            resp = await client.post(
                f"/api/v1/nodes/{node.id}/move",
                json={"parent_materialnode_id": str(target.id)},
            )
        assert resp.status_code == 404


class TestReorderNode:
    """POST /api/v1/nodes/{nid}/reorder"""

    async def test_reorder_success(self, client: AsyncClient) -> None:
        """Successful reorder returns updated node."""
        node = _mock_node(order=0)
        reordered = _mock_node(order=2)
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "reorder", return_value=reordered),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node.id}/reorder", json={"order": 2}
            )
        assert resp.status_code == 200
        assert resp.json()["order"] == 2

    async def test_negative_order_returns_422(self, client: AsyncClient) -> None:
        """Negative order is rejected with 422."""
        resp = await client.post(
            f"/api/v1/nodes/{uuid.uuid4()}/reorder", json={"order": -1}
        )
        assert resp.status_code == 422


class TestDeleteNode:
    """DELETE /api/v1/nodes/{nid}"""

    async def test_returns_204(self, client: AsyncClient) -> None:
        """Successful deletion returns 204 No Content."""
        node = _mock_node()
        tree_node = _mock_node(node_id=node.id)
        tree_node.materials = []
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(
                MaterialNodeRepository, "get_subtree", return_value=[tree_node]
            ),
            patch.object(MaterialNodeRepository, "delete", return_value=None),
        ):
            resp = await client.delete(f"/api/v1/nodes/{node.id}")
        assert resp.status_code == 204

    async def test_not_found_returns_404(self, client: AsyncClient) -> None:
        """Non-existent node returns 404."""
        with patch.object(MaterialNodeRepository, "get_by_id", return_value=None):
            resp = await client.delete(f"/api/v1/nodes/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_wrong_tenant_returns_404(self, client: AsyncClient) -> None:
        """Node belonging to different tenant returns 404."""
        node = _mock_node(tenant_id=uuid.uuid4())
        with patch.object(MaterialNodeRepository, "get_by_id", return_value=node):
            resp = await client.delete(f"/api/v1/nodes/{node.id}")
        assert resp.status_code == 404

    async def test_cleans_s3_files(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """S3 files from subtree materials are deleted after DB cascade."""
        entry = MagicMock()
        entry.source_url = "http://localhost:9000/bucket/tenants/t/file.pdf"
        node = _mock_node()
        tree_node = _mock_node(node_id=node.id)
        tree_node.materials = [entry]
        tree_node.children = []
        mock_s3.extract_key = MagicMock(return_value="tenants/t/file.pdf")
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(
                MaterialNodeRepository, "get_subtree", return_value=[tree_node]
            ),
            patch.object(MaterialNodeRepository, "delete", return_value=None),
        ):
            resp = await client.delete(f"/api/v1/nodes/{node.id}")
        assert resp.status_code == 204
        mock_s3.delete_object.assert_awaited_once_with("tenants/t/file.pdf")

    async def test_no_s3_cleanup_for_external_urls(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """External URLs (non-S3) are not deleted from S3."""
        entry = MagicMock()
        entry.source_url = "https://example.com/video.mp4"
        node = _mock_node()
        tree_node = _mock_node(node_id=node.id)
        tree_node.materials = [entry]
        tree_node.children = []
        mock_s3.extract_key = MagicMock(return_value=None)
        with (
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(
                MaterialNodeRepository, "get_subtree", return_value=[tree_node]
            ),
            patch.object(MaterialNodeRepository, "delete", return_value=None),
        ):
            resp = await client.delete(f"/api/v1/nodes/{node.id}")
        assert resp.status_code == 204
        mock_s3.delete_object.assert_not_awaited()
