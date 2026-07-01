"""Integration: GET /students roster + GET /students/{id}/enrollments (T5-BE).

Uses the FastAPI app with overridden ``get_session`` (test session) and
``get_current_tenant`` (stub tenant scoped to a freshly-seeded Tenant).
The seed builds one tenant (A) with three students exercising all three
credential states (active / revoked / credential-less) plus a second
tenant (B) for isolation checks; cleanup runs in reverse-FK order at
teardown.

Acceptance criteria (ratified pre-flight):
* tenant-scoped roster — only tenant A's students appear.
* roster-completeness — credential-less Student is listed, is_active=null.
* three is_active states — no-cred→null, active→true, revoked→false.
* enrollment_count — 2 enrollments→2, 0→0 (correlated subquery).
* dangling-preservation — enrollment to a soft-deleted course stays in
  GET enrollments (locks the reuse-``list_for_student`` decision).
* generic-404 cross-tenant — foreign student's enrollments → 404.
* PREP-guard — a CHECK-only key → 403 (not just no-scope).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
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


@pytest.fixture()
async def roster_seed(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Seed tenant A (3 students, 3 credential states) + tenant B (1 student).

    * ``s_active``   — credential is_active=True, enrolled in root_1 + root_2.
    * ``s_revoked``  — credential is_active=False, 0 enrollments.
    * ``s_credless`` — no credential, no display_name, 0 enrollments.
    * ``s_b``        — belongs to tenant B (isolation probe).
    """
    async with session_factory() as session:
        tenant_a = Tenant(name=f"t5-a-{uuid.uuid4().hex[:6]}")
        tenant_b = Tenant(name=f"t5-b-{uuid.uuid4().hex[:6]}")
        session.add_all([tenant_a, tenant_b])
        await session.flush()

        root_1 = make_root_course_node(
            tenant_id=tenant_a.id, title="Course One", order=0
        )
        root_2 = make_root_course_node(
            tenant_id=tenant_a.id, title="Course Two", order=1
        )
        session.add_all([root_1, root_2])
        await session.flush()

        s_active = Student(
            tenant_id=tenant_a.id, external_id="ext-active", display_name="Active One"
        )
        s_revoked = Student(
            tenant_id=tenant_a.id,
            external_id="ext-revoked",
            display_name="Revoked One",
        )
        s_credless = Student(
            tenant_id=tenant_a.id, external_id="ext-credless", display_name=None
        )
        s_b = Student(
            tenant_id=tenant_b.id, external_id="ext-b", display_name="Belongs To B"
        )
        session.add_all([s_active, s_revoked, s_credless, s_b])
        await session.flush()

        session.add_all(
            [
                StudentCredential(
                    student_id=s_active.id,
                    tenant_id=tenant_a.id,
                    login="active-login",
                    password_hash="seed-hash",
                    is_active=True,
                ),
                StudentCredential(
                    student_id=s_revoked.id,
                    tenant_id=tenant_a.id,
                    login="revoked-login",
                    password_hash="seed-hash",
                    is_active=False,
                ),
            ]
        )
        session.add_all(
            [
                StudentEnrollment(student_id=s_active.id, course_node_id=root_1.id),
                StudentEnrollment(student_id=s_active.id, course_node_id=root_2.id),
            ]
        )
        await session.commit()

        seed = {
            "tenant_a": tenant_a.id,
            "tenant_b": tenant_b.id,
            "root_1": root_1.id,
            "root_2": root_2.id,
            "s_active": s_active.id,
            "s_revoked": s_revoked.id,
            "s_credless": s_credless.id,
            "s_b": s_b.id,
        }

    yield seed

    async with session_factory() as session:
        for tid in (seed["tenant_a"], seed["tenant_b"]):
            student_ids = (
                select(Student.id).where(Student.tenant_id == tid).scalar_subquery()
            )
            await session.execute(
                delete(StudentEnrollment).where(
                    StudentEnrollment.student_id.in_(student_ids)
                )
            )
            await session.execute(
                delete(StudentCredential).where(StudentCredential.tenant_id == tid)
            )
            await session.execute(delete(Student).where(Student.tenant_id == tid))
            await session.execute(delete(CourseNode).where(CourseNode.tenant_id == tid))
            await session.execute(delete(Tenant).where(Tenant.id == tid))
        await session.commit()


@pytest.fixture()
async def roster_client(
    roster_seed: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient]:
    """FastAPI client wired to the test session, scoped to tenant A (prep+check)."""
    tenant_ctx = TenantContext(
        tenant_id=roster_seed["tenant_a"],
        tenant_name="t5-a",
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


# ── GET /students (roster) ─────────────────────────────────────────


class TestStudentRoster:
    async def test_tenant_scoped(
        self, roster_client: AsyncClient, roster_seed: dict[str, uuid.UUID]
    ) -> None:
        """Only tenant A's students appear; tenant B's is absent."""
        response = await roster_client.get("/api/v1/students")
        assert response.status_code == 200
        data = response.json()

        ids = {r["student_id"] for r in data["items"]}
        assert ids == {
            str(roster_seed["s_active"]),
            str(roster_seed["s_revoked"]),
            str(roster_seed["s_credless"]),
        }
        assert str(roster_seed["s_b"]) not in ids
        assert data["total"] == 3

    async def test_credless_student_present(
        self, roster_client: AsyncClient, roster_seed: dict[str, uuid.UUID]
    ) -> None:
        """Roster-completeness: a credential-less student is listed (null login)."""
        response = await roster_client.get("/api/v1/students")
        by_id = {r["student_id"]: r for r in response.json()["items"]}

        credless = by_id[str(roster_seed["s_credless"])]
        assert credless["login"] is None
        assert credless["is_active"] is None
        assert credless["display_name"] is None

    async def test_is_active_three_states(
        self, roster_client: AsyncClient, roster_seed: dict[str, uuid.UUID]
    ) -> None:
        """no-cred→null, active→true, revoked→false."""
        response = await roster_client.get("/api/v1/students")
        by_id = {r["student_id"]: r for r in response.json()["items"]}

        assert by_id[str(roster_seed["s_active"])]["is_active"] is True
        assert by_id[str(roster_seed["s_revoked"])]["is_active"] is False
        assert by_id[str(roster_seed["s_credless"])]["is_active"] is None

    async def test_enrollment_count(
        self, roster_client: AsyncClient, roster_seed: dict[str, uuid.UUID]
    ) -> None:
        """Correlated count: 2 enrollments→2, 0→0."""
        response = await roster_client.get("/api/v1/students")
        by_id = {r["student_id"]: r for r in response.json()["items"]}

        assert by_id[str(roster_seed["s_active"])]["enrollment_count"] == 2
        assert by_id[str(roster_seed["s_revoked"])]["enrollment_count"] == 0
        assert by_id[str(roster_seed["s_credless"])]["enrollment_count"] == 0

    async def test_prep_guard_check_only_forbidden(
        self, roster_client: AsyncClient, roster_seed: dict[str, uuid.UUID]
    ) -> None:
        """A CHECK-only key (not just no-scope) is rejected with 403."""
        check_only = TenantContext(
            tenant_id=roster_seed["tenant_a"],
            tenant_name="t5-a",
            scopes=["check"],
            plan_id="basic",
            key_prefix="cs_test",
        )
        app.dependency_overrides[get_current_tenant] = lambda: check_only

        response = await roster_client.get("/api/v1/students")
        assert response.status_code == 403


# ── GET /students/{id}/enrollments ─────────────────────────────────


class TestStudentEnrollments:
    async def test_returns_course_ids_and_time(
        self, roster_client: AsyncClient, roster_seed: dict[str, uuid.UUID]
    ) -> None:
        response = await roster_client.get(
            f"/api/v1/students/{roster_seed['s_active']}/enrollments"
        )
        assert response.status_code == 200
        data = response.json()

        assert data["student_id"] == str(roster_seed["s_active"])
        course_ids = {i["course_node_id"] for i in data["items"]}
        assert course_ids == {
            str(roster_seed["root_1"]),
            str(roster_seed["root_2"]),
        }
        assert all(i["enrolled_at"] for i in data["items"])

    async def test_dangling_enrollment_preserved(
        self,
        roster_client: AsyncClient,
        roster_seed: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A soft-deleted course keeps its enrollment row (admin unbind surface)."""
        async with session_factory() as session:
            await session.execute(
                update(CourseNode)
                .where(CourseNode.id == roster_seed["root_2"])
                .values(deleted_at=datetime.now(UTC))
            )
            await session.commit()

        response = await roster_client.get(
            f"/api/v1/students/{roster_seed['s_active']}/enrollments"
        )
        assert response.status_code == 200
        data = response.json()

        course_ids = {i["course_node_id"] for i in data["items"]}
        assert str(roster_seed["root_2"]) in course_ids
        assert len(data["items"]) == 2

    async def test_foreign_tenant_returns_404(
        self, roster_client: AsyncClient, roster_seed: dict[str, uuid.UUID]
    ) -> None:
        """A student in another tenant → 404 (existence not leaked)."""
        response = await roster_client.get(
            f"/api/v1/students/{roster_seed['s_b']}/enrollments"
        )
        assert response.status_code == 404
