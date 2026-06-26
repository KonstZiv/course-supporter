"""End-to-end integration: student portal auth + provisioning (Phase 6 T1).

Uses the FastAPI app with overridden ``get_session`` (real committed test
session) and ``get_current_tenant`` (stub PREP tenant scoped to a freshly
seeded Tenant). ``get_current_student`` is NOT overridden — the bearer flow is
exercised for real. Cleanup runs in reverse FK order at teardown.

Coverage:
* The acceptance flow: provision → login → /portal/me → revoke → both a new
  login AND the previously-issued bearer token are rejected (401).
* /portal/me without a token → 401.
* Provisioning: manual duplicate external_id → 409; weak password → 422;
  existing-Student mode attaches a credential.
* Enrollment: bind a root course → 201; bind a non-root node → 422;
  unbind → 204; unbind again → 404.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    CourseNode,
    Student,
    StudentCredential,
    StudentEnrollment,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_PASSWORD = "correct horse 10"


@pytest.fixture()
async def portal_seed(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Seed Tenant + root CourseNode + child node + one bare Student."""
    async with session_factory() as session:
        tenant = Tenant(name=f"portal-test-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        root = make_root_course_node(tenant_id=tenant.id, title="Course", order=0)
        session.add(root)
        await session.flush()

        child = CourseNode(tenant_id=tenant.id, parent_id=root.id, title="Module 1")
        session.add(child)

        bare = Student(tenant_id=tenant.id, external_id="integration-only")
        session.add(bare)
        await session.flush()
        await session.commit()

        seed = {
            "tenant_id": tenant.id,
            "root_id": root.id,
            "child_id": child.id,
            "bare_student_id": bare.id,
        }

    yield seed

    async with session_factory() as session:
        student_ids = (
            Student.__table__.select()
            .with_only_columns(Student.id)
            .where(Student.tenant_id == seed["tenant_id"])
            .scalar_subquery()
        )
        await session.execute(
            StudentEnrollment.__table__.delete().where(
                StudentEnrollment.student_id.in_(student_ids)
            )
        )
        await session.execute(
            StudentCredential.__table__.delete().where(
                StudentCredential.tenant_id == seed["tenant_id"]
            )
        )
        await session.execute(
            Student.__table__.delete().where(Student.tenant_id == seed["tenant_id"])
        )
        await session.execute(
            CourseNode.__table__.delete().where(
                CourseNode.tenant_id == seed["tenant_id"]
            )
        )
        await session.execute(
            Tenant.__table__.delete().where(Tenant.id == seed["tenant_id"])
        )
        await session.commit()


@pytest.fixture()
async def portal_client(
    portal_seed: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient]:
    """FastAPI client: real committed session + stub PREP tenant."""
    tenant_ctx = TenantContext(
        tenant_id=portal_seed["tenant_id"],
        tenant_name="portal-test",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_test",
    )

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_current_tenant] = lambda: tenant_ctx

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


async def _provision(
    client: AsyncClient, login: str, **extra: object
) -> dict[str, object]:
    body: dict[str, object] = {
        "mode": "generate",
        "login": login,
        "password": _PASSWORD,
        **extra,
    }
    resp = await client.post("/api/v1/students", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestAcceptanceFlow:
    async def test_provision_login_me_revoke(
        self, portal_client: AsyncClient, portal_seed: dict[str, uuid.UUID]
    ) -> None:
        tenant_id = str(portal_seed["tenant_id"])
        provisioned = await _provision(portal_client, "alice", display_name="Alice A.")
        student_id = provisioned["student_id"]

        # login → token
        login_body = {"tenant_id": tenant_id, "login": "alice", "password": _PASSWORD}
        login = await portal_client.post("/api/v1/portal/login", json=login_body)
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        assert login.json()["display_name"] == "Alice A."

        # token grants access to the protected endpoint
        me = await portal_client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert me.status_code == 200, me.text
        assert me.json()["login"] == "alice"
        assert me.json()["student_id"] == student_id

        # revoke access
        revoke = await portal_client.post(f"/api/v1/students/{student_id}/revoke")
        assert revoke.status_code == 204, revoke.text

        # both a fresh login AND the existing token are now rejected
        relogin = await portal_client.post("/api/v1/portal/login", json=login_body)
        assert relogin.status_code == 401

        me_after = await portal_client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert me_after.status_code == 401

    async def test_restore_reenables_login(
        self, portal_client: AsyncClient, portal_seed: dict[str, uuid.UUID]
    ) -> None:
        tenant_id = str(portal_seed["tenant_id"])
        provisioned = await _provision(portal_client, "bob")
        student_id = provisioned["student_id"]
        login_body = {"tenant_id": tenant_id, "login": "bob", "password": _PASSWORD}

        await portal_client.post(f"/api/v1/students/{student_id}/revoke")
        assert (
            await portal_client.post("/api/v1/portal/login", json=login_body)
        ).status_code == 401

        restore = await portal_client.post(f"/api/v1/students/{student_id}/restore")
        assert restore.status_code == 204
        assert (
            await portal_client.post("/api/v1/portal/login", json=login_body)
        ).status_code == 200


class TestAuthEdges:
    async def test_me_without_token_401(self, portal_client: AsyncClient) -> None:
        resp = await portal_client.get("/api/v1/portal/me")
        assert resp.status_code == 401

    async def test_wrong_password_401(
        self, portal_client: AsyncClient, portal_seed: dict[str, uuid.UUID]
    ) -> None:
        await _provision(portal_client, "carol")
        resp = await portal_client.post(
            "/api/v1/portal/login",
            json={
                "tenant_id": str(portal_seed["tenant_id"]),
                "login": "carol",
                "password": "wrong password!!",
            },
        )
        assert resp.status_code == 401


class TestProvisioning:
    async def test_weak_password_422(self, portal_client: AsyncClient) -> None:
        resp = await portal_client.post(
            "/api/v1/students",
            json={"mode": "generate", "login": "dave", "password": "short"},
        )
        assert resp.status_code == 422

    async def test_manual_duplicate_external_id_409(
        self, portal_client: AsyncClient
    ) -> None:
        body = {
            "mode": "manual",
            "external_id": "ext-dup",
            "login": "eve",
            "password": _PASSWORD,
        }
        first = await portal_client.post("/api/v1/students", json=body)
        assert first.status_code == 201
        body["login"] = "eve2"
        second = await portal_client.post("/api/v1/students", json=body)
        assert second.status_code == 409

    async def test_existing_mode_attaches_credential(
        self, portal_client: AsyncClient, portal_seed: dict[str, uuid.UUID]
    ) -> None:
        bare_id = str(portal_seed["bare_student_id"])
        resp = await portal_client.post(
            "/api/v1/students",
            json={
                "mode": "existing",
                "student_id": bare_id,
                "login": "frank",
                "password": _PASSWORD,
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["student_id"] == bare_id
        assert resp.json()["external_id"] == "integration-only"

        login = await portal_client.post(
            "/api/v1/portal/login",
            json={
                "tenant_id": str(portal_seed["tenant_id"]),
                "login": "frank",
                "password": _PASSWORD,
            },
        )
        assert login.status_code == 200


class TestEnrollment:
    async def test_bind_unbind_root(
        self, portal_client: AsyncClient, portal_seed: dict[str, uuid.UUID]
    ) -> None:
        provisioned = await _provision(portal_client, "grace")
        student_id = provisioned["student_id"]
        root_id = str(portal_seed["root_id"])

        bind = await portal_client.post(
            "/api/v1/students/enrollments",
            json={"student_id": student_id, "course_node_id": root_id},
        )
        assert bind.status_code == 201, bind.text

        # duplicate bind → 409
        dup = await portal_client.post(
            "/api/v1/students/enrollments",
            json={"student_id": student_id, "course_node_id": root_id},
        )
        assert dup.status_code == 409

        unbind = await portal_client.delete(
            "/api/v1/students/enrollments",
            params={"student_id": student_id, "course_node_id": root_id},
        )
        assert unbind.status_code == 204

        unbind_again = await portal_client.delete(
            "/api/v1/students/enrollments",
            params={"student_id": student_id, "course_node_id": root_id},
        )
        assert unbind_again.status_code == 404

    async def test_bind_non_root_422(
        self, portal_client: AsyncClient, portal_seed: dict[str, uuid.UUID]
    ) -> None:
        provisioned = await _provision(portal_client, "heidi")
        resp = await portal_client.post(
            "/api/v1/students/enrollments",
            json={
                "student_id": provisioned["student_id"],
                "course_node_id": str(portal_seed["child_id"]),
            },
        )
        assert resp.status_code == 422
