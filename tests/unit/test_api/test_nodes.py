"""Tests for material tree node API endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.repositories import CourseRepository

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
    course_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    title: str = "Test Node",
    description: str | None = None,
    order: int = 0,
    children: list[object] | None = None,
) -> MagicMock:
    """Create a mock MaterialNode with ORM-compatible attributes."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.course_id = course_id or uuid.uuid4()
    node.parent_id = parent_id
    node.title = title
    node.description = description
    node.order = order
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
def course_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


def _mock_course(course_id: uuid.UUID) -> MagicMock:
    """Create a mock Course that passes tenant isolation."""
    course = MagicMock()
    course.id = course_id
    course.tenant_id = STUB_TENANT.tenant_id
    return course


class TestCreateRootNode:
    """POST /api/v1/courses/{id}/nodes"""

    async def test_returns_201(self, client: AsyncClient, course_id: uuid.UUID) -> None:
        """Successful root node creation returns 201."""
        node = _mock_node(course_id=course_id)
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "create", return_value=node),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes",
                json={"title": "Module 1"},
            )
        assert resp.status_code == 201

    async def test_returns_node_fields(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Response contains all expected node fields."""
        node = _mock_node(course_id=course_id, title="Module 1")
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "create", return_value=node),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes",
                json={"title": "Module 1"},
            )
        data = resp.json()
        assert data["id"] == str(node.id)
        assert data["title"] == "Module 1"
        assert data["parent_id"] is None
        assert data["course_id"] == str(course_id)
        assert "created_at" in data
        assert "updated_at" in data

    async def test_with_description(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Root node accepts optional description."""
        node = _mock_node(course_id=course_id, description="Details")
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "create", return_value=node),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes",
                json={"title": "Mod", "description": "Details"},
            )
        assert resp.status_code == 201
        assert resp.json()["description"] == "Details"

    async def test_empty_title_returns_422(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Empty title is rejected with 422."""
        with patch.object(
            CourseRepository, "get_by_id", return_value=_mock_course(course_id)
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes",
                json={"title": ""},
            )
        assert resp.status_code == 422

    async def test_missing_title_returns_422(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Missing title is rejected with 422."""
        with patch.object(
            CourseRepository, "get_by_id", return_value=_mock_course(course_id)
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes",
                json={},
            )
        assert resp.status_code == 422

    async def test_course_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent course returns 404."""
        with patch.object(CourseRepository, "get_by_id", return_value=None):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes",
                json={"title": "Module 1"},
            )
        assert resp.status_code == 404
        assert "Course not found" in resp.json()["detail"]


class TestCreateChildNode:
    """POST /api/v1/courses/{id}/nodes/{nid}/children"""

    async def test_returns_201(self, client: AsyncClient, course_id: uuid.UUID) -> None:
        """Successful child creation returns 201."""
        parent = _mock_node(course_id=course_id, title="Parent")
        child = _mock_node(course_id=course_id, parent_id=parent.id, title="Child")
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(
                MaterialNodeRepository,
                "get_by_id",
                return_value=parent,
            ),
            patch.object(MaterialNodeRepository, "create", return_value=child),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{parent.id}/children",
                json={"title": "Child"},
            )
        assert resp.status_code == 201
        assert resp.json()["parent_id"] == str(parent.id)

    async def test_parent_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent parent returns 404."""
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=None),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{uuid.uuid4()}/children",
                json={"title": "Child"},
            )
        assert resp.status_code == 404
        assert "Node not found" in resp.json()["detail"]

    async def test_parent_wrong_course_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Parent belonging to a different course returns 404."""
        other_course = uuid.uuid4()
        parent = _mock_node(course_id=other_course, title="Wrong course parent")
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=parent),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{parent.id}/children",
                json={"title": "Child"},
            )
        assert resp.status_code == 404


class TestGetTree:
    """GET /api/v1/courses/{id}/nodes/tree"""

    async def test_returns_empty_list(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Course with no nodes returns empty list."""
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_tree", return_value=[]),
        ):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/nodes/tree",
            )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_nested_tree(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Tree with parent-child structure returned nested."""
        child = _mock_node(course_id=course_id, title="Child")
        root = _mock_node(course_id=course_id, title="Root", children=[child])
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_tree", return_value=[root]),
        ):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/nodes/tree",
            )
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Root"
        assert len(data[0]["children"]) == 1
        assert data[0]["children"][0]["title"] == "Child"

    async def test_course_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent course returns 404."""
        with patch.object(CourseRepository, "get_by_id", return_value=None):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/nodes/tree",
            )
        assert resp.status_code == 404


class TestGetNode:
    """GET /api/v1/courses/{id}/nodes/{nid}"""

    async def test_returns_node(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Existing node returned with correct fields."""
        node = _mock_node(course_id=course_id, title="Node 1", order=2)
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
        ):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/nodes/{node.id}",
            )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Node 1"
        assert resp.json()["order"] == 2

    async def test_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent node returns 404."""
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=None),
        ):
            resp = await client.get(
                f"/api/v1/courses/{course_id}/nodes/{uuid.uuid4()}",
            )
        assert resp.status_code == 404


class TestUpdateNode:
    """PATCH /api/v1/courses/{id}/nodes/{nid}"""

    async def test_update_title(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Title updated when provided."""
        node = _mock_node(course_id=course_id, title="Old")
        updated = _mock_node(course_id=course_id, title="New Title")
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "update", return_value=updated),
        ):
            resp = await client.patch(
                f"/api/v1/courses/{course_id}/nodes/{node.id}",
                json={"title": "New Title"},
            )
        assert resp.status_code == 200
        assert resp.json()["title"] == "New Title"

    async def test_clear_description(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Description cleared when set to null."""
        node = _mock_node(course_id=course_id, description="Old desc")
        updated = _mock_node(course_id=course_id, description=None)
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "update", return_value=updated),
        ):
            resp = await client.patch(
                f"/api/v1/courses/{course_id}/nodes/{node.id}",
                json={"description": None},
            )
        assert resp.status_code == 200
        assert resp.json()["description"] is None

    async def test_empty_body_is_valid(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Empty body (no fields to update) is accepted."""
        node = _mock_node(course_id=course_id)
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "update", return_value=node),
        ):
            resp = await client.patch(
                f"/api/v1/courses/{course_id}/nodes/{node.id}",
                json={},
            )
        assert resp.status_code == 200

    async def test_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent node returns 404."""
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=None),
        ):
            resp = await client.patch(
                f"/api/v1/courses/{course_id}/nodes/{uuid.uuid4()}",
                json={"title": "New"},
            )
        assert resp.status_code == 404


class TestMoveNode:
    """POST /api/v1/courses/{id}/nodes/{nid}/move"""

    async def test_move_to_new_parent(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Node moved to a new parent."""
        node = _mock_node(course_id=course_id, title="Movable")
        target = _mock_node(course_id=course_id, title="Target")
        moved = _mock_node(course_id=course_id, parent_id=target.id)

        def fake_get(nid: uuid.UUID) -> MagicMock | None:
            lookup = {node.id: node, target.id: target}
            return lookup.get(nid)

        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", side_effect=fake_get),
            patch.object(MaterialNodeRepository, "move", return_value=moved),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node.id}/move",
                json={"parent_id": str(target.id)},
            )
        assert resp.status_code == 200
        assert resp.json()["parent_id"] == str(target.id)

    async def test_move_to_root(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Node moved to root (parent_id=null)."""
        node = _mock_node(course_id=course_id, parent_id=uuid.uuid4())
        moved = _mock_node(course_id=course_id, parent_id=None)
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "move", return_value=moved),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node.id}/move",
                json={"parent_id": None},
            )
        assert resp.status_code == 200
        assert resp.json()["parent_id"] is None

    async def test_cycle_returns_422(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Cycle detection error returns 422."""
        node = _mock_node(course_id=course_id)
        target = _mock_node(course_id=course_id)

        def fake_get(nid: uuid.UUID) -> MagicMock | None:
            lookup = {node.id: node, target.id: target}
            return lookup.get(nid)

        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", side_effect=fake_get),
            patch.object(
                MaterialNodeRepository,
                "move",
                side_effect=ValueError("would create a cycle"),
            ),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node.id}/move",
                json={"parent_id": str(target.id)},
            )
        assert resp.status_code == 422
        assert "cycle" in resp.json()["detail"]

    async def test_target_wrong_course_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Target parent in different course returns 404."""
        node = _mock_node(course_id=course_id)
        target = _mock_node(course_id=uuid.uuid4())  # different course

        def fake_get(nid: uuid.UUID) -> MagicMock | None:
            lookup = {node.id: node, target.id: target}
            return lookup.get(nid)

        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", side_effect=fake_get),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node.id}/move",
                json={"parent_id": str(target.id)},
            )
        assert resp.status_code == 404


class TestReorderNode:
    """POST /api/v1/courses/{id}/nodes/{nid}/reorder"""

    async def test_reorder_success(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Successful reorder returns updated node."""
        node = _mock_node(course_id=course_id, order=0)
        reordered = _mock_node(course_id=course_id, order=2)
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "reorder", return_value=reordered),
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node.id}/reorder",
                json={"order": 2},
            )
        assert resp.status_code == 200
        assert resp.json()["order"] == 2

    async def test_negative_order_returns_422(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Negative order is rejected with 422."""
        with patch.object(
            CourseRepository, "get_by_id", return_value=_mock_course(course_id)
        ):
            resp = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{uuid.uuid4()}/reorder",
                json={"order": -1},
            )
        assert resp.status_code == 422


class TestDeleteNode:
    """DELETE /api/v1/courses/{id}/nodes/{nid}"""

    async def test_returns_204(self, client: AsyncClient, course_id: uuid.UUID) -> None:
        """Successful deletion returns 204 No Content."""
        node = _mock_node(course_id=course_id)
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=node),
            patch.object(MaterialNodeRepository, "delete", return_value=None),
        ):
            resp = await client.delete(
                f"/api/v1/courses/{course_id}/nodes/{node.id}",
            )
        assert resp.status_code == 204

    async def test_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent node returns 404."""
        with (
            patch.object(
                CourseRepository, "get_by_id", return_value=_mock_course(course_id)
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=None),
        ):
            resp = await client.delete(
                f"/api/v1/courses/{course_id}/nodes/{uuid.uuid4()}",
            )
        assert resp.status_code == 404

    async def test_course_not_found_returns_404(
        self, client: AsyncClient, course_id: uuid.UUID
    ) -> None:
        """Non-existent course returns 404."""
        with patch.object(CourseRepository, "get_by_id", return_value=None):
            resp = await client.delete(
                f"/api/v1/courses/{course_id}/nodes/{uuid.uuid4()}",
            )
        assert resp.status_code == 404
