"""A-BE-2 (№21): GET /api/v1/documents/{id} projects the stored course_root_id.

Real PostgreSQL. Builds a two-level tree (root course node → nested section)
and asserts the endpoint returns the KD-delta denormalised root the repository
resolved at document creation — the root for a nested-node document, and the
node itself for a root-node document.

Requires ``docker compose up -d`` (PostgreSQL).
Run: ``uv run pytest tests/integration/test_document_course_root.py --run-db -v``
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import AuthoredDocument, CourseNode, Tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


@pytest.fixture()
async def tree_env(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Tenant + root course node + nested section, one document on each.

    Documents are created through ``AuthoredDocumentRepository.create`` so the
    real parent-walk resolver (``_resolve_course_root_id`` → ``get_root_for``)
    fills ``course_root_id`` — no hand-set value.
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"root-proj-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        root = make_root_course_node(tenant_id=tenant.id, title="Course", order=0)
        session.add(root)
        await session.flush()

        section = CourseNode(
            tenant_id=tenant.id,
            parent_id=root.id,
            title="Section",
            order=0,
        )
        session.add(section)
        await session.flush()

        repo = AuthoredDocumentRepository(session)
        nested_doc = await repo.create(
            node_id=section.id, source_type="text", source_url="s3://nested"
        )
        root_doc = await repo.create(
            node_id=root.id, source_type="text", source_url="s3://root"
        )
        await session.commit()

        ids = {
            "tenant_id": tenant.id,
            "root_id": root.id,
            "section_id": section.id,
            "nested_doc_id": nested_doc.id,
            "root_doc_id": root_doc.id,
        }

    yield ids

    async with session_factory() as session:
        await session.execute(
            delete(AuthoredDocument).where(
                AuthoredDocument.course_node_id.in_([ids["root_id"], ids["section_id"]])
            )
        )
        await session.execute(
            delete(CourseNode).where(CourseNode.id == ids["section_id"])
        )
        await session.execute(delete(CourseNode).where(CourseNode.id == ids["root_id"]))
        await session.execute(delete(Tenant).where(Tenant.id == ids["tenant_id"]))
        await session.commit()


@pytest.fixture()
def _wire(
    tree_env: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> Generator[None]:
    tenant = TenantContext(
        tenant_id=tree_env["tenant_id"],
        tenant_name="root-proj",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_test",
    )

    async def _override_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_tenant() -> TenantContext:
        return tenant

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_tenant] = _override_tenant
    yield
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_nested_document_reports_course_root_not_its_node(
    _wire: None, tree_env: dict[str, uuid.UUID]
) -> None:
    """A document on a nested node reports the course root, not its own node."""
    async with _client() as client:
        resp = await client.get(f"/api/v1/documents/{tree_env['nested_doc_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["course_node_id"] == str(tree_env["section_id"])
    assert body["course_root_id"] == str(tree_env["root_id"])
    assert body["course_root_id"] != body["course_node_id"]


async def test_root_document_reports_course_node_id_as_root(
    _wire: None, tree_env: dict[str, uuid.UUID]
) -> None:
    """A document on the root node reports course_root_id == course_node_id."""
    async with _client() as client:
        resp = await client.get(f"/api/v1/documents/{tree_env['root_doc_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["course_node_id"] == str(tree_env["root_id"])
    assert body["course_root_id"] == str(tree_env["root_id"])
    assert body["course_root_id"] == body["course_node_id"]
