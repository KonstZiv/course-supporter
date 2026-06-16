"""Regression test for post-commit ORM serialization with server-generated columns.

Surfaced during Phase 2.1 C5 Pivot 1 smoke setup: ``POST /api/v1/nodes``
returned 500 because ``CourseNode.updated_at`` (``server_default=func.now()``
+ ``onupdate=func.now()``) was marked stale after ``session.flush()`` and
the subsequent ``UPDATE course_nodes SET content_hash=...`` from
``ContentHashService.invalidate_up``. Pydantic ``model_validate(node)``
walked attrs synchronously in ``from_attributes`` mode and triggered a
lazy refresh on the expired ``updated_at`` column outside the async
greenlet → ``MissingGreenlet``.

Fix: ``Base.__mapper_args__ = {"eager_defaults": True}`` — SQLAlchemy
populates server-generated values via ``RETURNING`` at flush time.

The pre-existing ``tests/unit/test_api/test_nodes.py`` mocks the session
entirely (``MagicMock``), so the SA expire-and-lazy-load path is never
exercised. This test runs the real route handler against a real Postgres
session to ensure the regression cannot return silently.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import CourseNode, Tenant

pytestmark = [pytest.mark.asyncio, pytest.mark.requires_db]


@pytest.fixture()
async def seeded_tenant_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[uuid.UUID]:
    """Create a Tenant via real commit; delete subtree on teardown."""
    async with session_factory() as session:
        tenant = Tenant(name=f"regression-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
        await session.commit()

    yield tenant_id

    async with session_factory() as session:
        await session.execute(
            CourseNode.__table__.delete().where(CourseNode.tenant_id == tenant_id)
        )
        await session.execute(Tenant.__table__.delete().where(Tenant.id == tenant_id))
        await session.commit()


@pytest.fixture()
async def node_client(
    seeded_tenant_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient]:
    """FastAPI client with real DB session + stub tenant context."""
    tenant_ctx = TenantContext(
        tenant_id=seeded_tenant_id,
        tenant_name="regression",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_test",
    )

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_current_tenant] = lambda: tenant_ctx

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


async def test_create_root_node_returns_serializable_response(
    node_client: AsyncClient,
) -> None:
    """POST /api/v1/nodes returns 201 with non-empty ``updated_at`` ISO timestamp.

    Without ``eager_defaults=True`` on ``Base``, the server-generated
    ``updated_at`` value is not loaded onto the in-memory ``CourseNode``
    instance after ``session.flush()`` + ``UPDATE`` from
    ``ContentHashService.invalidate_up``. Pydantic ``from_attributes``
    serialization then triggers a sync lazy-load → ``MissingGreenlet``,
    and the route returns 500.

    Asserts:
    * HTTP 201.
    * Response body's ``updated_at`` is a non-empty string parseable
      as an ISO 8601 datetime.
    * ``created_at`` is likewise present and parseable.
    """
    response = await node_client.post(
        "/api/v1/nodes",
        json={"title": "Regression serialization probe", "default_language": "uk"},
    )
    assert response.status_code == 201, response.text

    payload = response.json()
    assert payload["title"] == "Regression serialization probe"

    updated_at = payload["updated_at"]
    assert isinstance(updated_at, str) and updated_at, (
        f"updated_at must be non-empty ISO string: {payload!r}"
    )
    datetime.fromisoformat(updated_at)

    created_at = payload["created_at"]
    assert isinstance(created_at, str) and created_at, (
        f"created_at must be non-empty ISO string: {payload!r}"
    )
    datetime.fromisoformat(created_at)


async def test_detail_serializes_default_language(
    node_client: AsyncClient,
) -> None:
    """GET /nodes/{id}/detail carries ``default_language`` (Task 3.2.5b commit-0).

    The tree-feed (``NodeWithDocumentsResponse``) previously omitted
    ``default_language``, so the UI language badge fell back to "auto"
    on initial load even for a course with a stored language. The field
    is now serialized from ``CourseNode.default_language`` via
    ``from_attributes``.

    Asserts:
    * Root node serializes the stored, normalized language (``"ukr"``) —
      roots are guaranteed non-null by the
      ``course_nodes_root_language_required`` CHECK constraint.
    * A child node created without an explicit language serializes
      ``default_language = null`` (inheritance is resolved at ingestion
      from the root, not stored) — tolerated, not an error.
    """
    root_resp = await node_client.post(
        "/api/v1/nodes",
        json={"title": "Lang detail root", "default_language": "ukr"},
    )
    assert root_resp.status_code == 201, root_resp.text
    root_id = root_resp.json()["id"]

    child_resp = await node_client.post(
        f"/api/v1/nodes/{root_id}/children",
        json={"title": "Lang detail child"},
    )
    assert child_resp.status_code == 201, child_resp.text
    child_id = child_resp.json()["id"]

    detail_resp = await node_client.get(f"/api/v1/nodes/{root_id}/detail")
    assert detail_resp.status_code == 200, detail_resp.text
    detail = detail_resp.json()

    assert detail["default_language"] == "ukr", (
        f"root must serialize stored language, got: {detail!r}"
    )

    children = detail["children"]
    assert len(children) == 1, f"expected one child, got: {children!r}"
    child = children[0]
    assert child["id"] == child_id
    assert child["default_language"] is None, (
        f"child without explicit language must serialize null, got: {child!r}"
    )

    # Task 3.2.5b commit-1: the tree-feed carries summary badge state.
    # These nodes have no Raw (never generated) → ``none`` / ``false`` at
    # the route boundary, on both root and child (recursive composition).
    assert detail["summary_status"] == "none", detail
    assert detail["materials_changed"] is False, detail
    assert child["summary_status"] == "none", child
    assert child["materials_changed"] is False, child
