"""End-to-end integration: student portal auth + provisioning (Phase 6 T1).

Uses the FastAPI app with overridden ``get_session`` (real committed test
session) and ``get_current_tenant`` (stub PREP tenant scoped to a freshly
seeded Tenant). ``get_current_student`` is NOT overridden — the bearer flow is
exercised for real. Cleanup runs in reverse FK order at teardown.

Coverage:
* The acceptance flow: provision → login → /portal/me → revoke → both a new
  login AND the previously-issued bearer token are rejected (401).
* /portal/me without a token → 401.
* /portal/me carries the standing review-language preference, null until the
  student asks for one (step Г2 §1.1).
* The portal pages' language (M-3): PUT /portal/me/interface-language keeps
  it on the student, /portal/me and login serve it back; it stays apart from
  the review language in both directions.
* Provisioning: manual duplicate external_id → 409; weak password → 422;
  existing-Student mode attaches a credential.
* Enrollment: bind a root course → 201; bind a non-root node → 422;
  unbind → 204; unbind again → 404.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSummary,
    HomeworkSubmission,
    Job,
    Student,
    StudentCredential,
    StudentEnrollment,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
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
            delete(StudentEnrollment)
            .where(StudentEnrollment.student_id.in_(student_ids))
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            delete(StudentCredential).where(
                StudentCredential.tenant_id == seed["tenant_id"]
            )
        )
        await session.execute(
            delete(Student).where(Student.tenant_id == seed["tenant_id"])
        )
        await session.execute(
            delete(CourseNode).where(CourseNode.tenant_id == seed["tenant_id"])
        )
        await session.execute(delete(Tenant).where(Tenant.id == seed["tenant_id"]))
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
    provisioned: dict[str, object] = resp.json()
    return provisioned


async def _login(client: AsyncClient, seed: dict[str, uuid.UUID], login: str) -> str:
    resp = await client.post(
        "/api/v1/portal/login",
        json={
            "tenant_id": str(seed["tenant_id"]),
            "login": login,
            "password": _PASSWORD,
        },
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


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


class TestPreferredLanguageReadBack:
    """The standing preference is readable, not just writable (step Г2 §1.1).

    ``submission_core`` writes ``students.preferred_language`` when a student
    asks for a language explicitly; until now nothing served it back, so the
    form could not open on the student's own standing choice. This is the
    read side, exercised through the real bearer flow rather than a stub —
    the value travels session context → schema, and a stub context would
    prove neither leg.
    """

    async def test_null_until_the_student_asks(
        self, portal_client: AsyncClient, portal_seed: dict[str, uuid.UUID]
    ) -> None:
        await _provision(portal_client, "erin")
        token = await _login(portal_client, portal_seed, "erin")
        me = await portal_client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert me.status_code == 200, me.text
        assert me.json()["preferred_language"] is None

    async def test_serves_the_stored_code(
        self,
        portal_client: AsyncClient,
        portal_seed: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provisioned = await _provision(portal_client, "frank")
        student_id = uuid.UUID(str(provisioned["student_id"]))
        # Write the column the way a submission with an explicit language
        # would (``StudentRepository.set_preferred_language``), then read it
        # back through the route — the point under test is the read path.
        async with session_factory() as session:
            await StudentRepository(session).set_preferred_language(student_id, "eng")
            await session.commit()

        token = await _login(portal_client, portal_seed, "frank")
        me = await portal_client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert me.status_code == 200, me.text
        assert me.json()["preferred_language"] == "eng"


_INTERFACE_LANGUAGE = "/api/v1/portal/me/interface-language"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _stored_languages(
    session_factory: async_sessionmaker[AsyncSession], student_id: object
) -> tuple[str | None, str | None]:
    """``(interface_language, preferred_language)`` as the row holds them."""
    async with session_factory() as session:
        student = await session.get(Student, uuid.UUID(str(student_id)))
    assert student is not None
    return student.interface_language, student.preferred_language


@pytest.fixture()
async def file_task(
    portal_seed: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[uuid.UUID]:
    """A ready task answered with a file, in the seeded course.

    Torn down before ``portal_seed``: the submissions and jobs a test leaves
    first, then the task itself.
    """
    async with session_factory() as session:
        document = AuthoredDocument(
            course_node_id=portal_seed["root_id"],
            course_root_id=portal_seed["root_id"],
            source_type="text",
            source_url="https://example.test/task",
            task_type="task",
            language="ukr",
            content_hash="a3" + "0" * 62,
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentSummary(
                authored_document_id=document.id,
                course_root_id=portal_seed["root_id"],
                title="Task",
                status="ready",
            )
        )
        await session.commit()
        task_id = document.id

    yield task_id

    async with session_factory() as session:
        tenant_id = portal_seed["tenant_id"]
        await session.execute(
            delete(HomeworkSubmission).where(HomeworkSubmission.tenant_id == tenant_id)
        )
        await session.execute(delete(Job).where(Job.tenant_id == tenant_id))
        await session.execute(
            delete(DocumentSummary).where(
                DocumentSummary.authored_document_id == task_id
            )
        )
        await session.execute(
            delete(AuthoredDocument).where(AuthoredDocument.id == task_id)
        )
        await session.commit()


class TestInterfaceLanguage:
    """The portal pages' language is kept on the student (M-3).

    Exercised through the real bearer flow: PUT writes the column, ``/me``
    and login read it back. It is a field of its own — it neither reads nor
    writes the review language, and a submission in another review language
    leaves it as it was.
    """

    async def test_me_is_null_then_serves_the_stored_code(
        self, portal_client: AsyncClient, portal_seed: dict[str, uuid.UUID]
    ) -> None:
        await _provision(portal_client, "ivan")
        token = await _login(portal_client, portal_seed, "ivan")

        me = await portal_client.get("/api/v1/portal/me", headers=_bearer(token))
        assert me.status_code == 200, me.text
        assert me.json()["interface_language"] is None

        put = await portal_client.put(
            _INTERFACE_LANGUAGE, json={"language": "en"}, headers=_bearer(token)
        )
        assert put.status_code == 200, put.text

        me = await portal_client.get("/api/v1/portal/me", headers=_bearer(token))
        assert me.status_code == 200, me.text
        assert me.json()["interface_language"] == "eng"

    async def test_login_serves_the_stored_code(
        self, portal_client: AsyncClient, portal_seed: dict[str, uuid.UUID]
    ) -> None:
        await _provision(portal_client, "judy")
        login_body = {
            "tenant_id": str(portal_seed["tenant_id"]),
            "login": "judy",
            "password": _PASSWORD,
        }
        first = await portal_client.post("/api/v1/portal/login", json=login_body)
        assert first.status_code == 200, first.text
        assert first.json()["interface_language"] is None

        put = await portal_client.put(
            _INTERFACE_LANGUAGE,
            json={"language": "ukr"},
            headers=_bearer(first.json()["access_token"]),
        )
        assert put.status_code == 200, put.text

        second = await portal_client.post("/api/v1/portal/login", json=login_body)
        assert second.status_code == 200, second.text
        assert second.json()["interface_language"] == "ukr"

    @pytest.mark.parametrize(
        ("given", "stored"),
        [pytest.param("en", "eng", id="639-1"), pytest.param("ukr", "ukr", id="639-3")],
    )
    async def test_put_stores_the_canonical_code(
        self,
        portal_client: AsyncClient,
        portal_seed: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        given: str,
        stored: str,
    ) -> None:
        provisioned = await _provision(portal_client, "kate")
        token = await _login(portal_client, portal_seed, "kate")

        put = await portal_client.put(
            _INTERFACE_LANGUAGE, json={"language": given}, headers=_bearer(token)
        )
        assert put.status_code == 200, put.text
        assert put.json() == {"interface_language": stored}
        interface, _ = await _stored_languages(
            session_factory, provisioned["student_id"]
        )
        assert interface == stored

        again = await portal_client.put(
            _INTERFACE_LANGUAGE, json={"language": given}, headers=_bearer(token)
        )
        assert again.status_code == 200, again.text
        assert again.json() == {"interface_language": stored}
        assert await _stored_languages(session_factory, provisioned["student_id"]) == (
            stored,
            None,
        )

    @pytest.mark.parametrize(
        "language",
        [pytest.param("xx", id="unknown"), pytest.param("kor", id="not-allowed")],
    )
    async def test_put_refuses_a_bad_language_and_keeps_the_stored_one(
        self,
        portal_client: AsyncClient,
        portal_seed: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        language: str,
    ) -> None:
        provisioned = await _provision(portal_client, "leo")
        token = await _login(portal_client, portal_seed, "leo")
        set_first = await portal_client.put(
            _INTERFACE_LANGUAGE, json={"language": "ukr"}, headers=_bearer(token)
        )
        assert set_first.status_code == 200, set_first.text

        put = await portal_client.put(
            _INTERFACE_LANGUAGE, json={"language": language}, headers=_bearer(token)
        )
        assert put.status_code == 422, put.text
        interface, _ = await _stored_languages(
            session_factory, provisioned["student_id"]
        )
        assert interface == "ukr"

    async def test_put_without_a_token_is_401(self, portal_client: AsyncClient) -> None:
        put = await portal_client.put(_INTERFACE_LANGUAGE, json={"language": "eng"})
        assert put.status_code == 401

    async def test_put_changes_only_the_student_of_the_token(
        self,
        portal_client: AsyncClient,
        portal_seed: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        student_a = await _provision(portal_client, "mia")
        student_b = await _provision(portal_client, "ned")
        token_a = await _login(portal_client, portal_seed, "mia")

        put = await portal_client.put(
            _INTERFACE_LANGUAGE, json={"language": "eng"}, headers=_bearer(token_a)
        )
        assert put.status_code == 200, put.text

        assert await _stored_languages(session_factory, student_a["student_id"]) == (
            "eng",
            None,
        )
        assert await _stored_languages(session_factory, student_b["student_id"]) == (
            None,
            None,
        )

    async def test_put_leaves_the_review_language_alone(
        self,
        portal_client: AsyncClient,
        portal_seed: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provisioned = await _provision(portal_client, "olga")
        student_id = uuid.UUID(str(provisioned["student_id"]))
        async with session_factory() as session:
            await StudentRepository(session).set_preferred_language(student_id, "ukr")
            await session.commit()
        token = await _login(portal_client, portal_seed, "olga")

        put = await portal_client.put(
            _INTERFACE_LANGUAGE, json={"language": "eng"}, headers=_bearer(token)
        )
        assert put.status_code == 200, put.text

        assert await _stored_languages(session_factory, student_id) == ("eng", "ukr")
        me = await portal_client.get("/api/v1/portal/me", headers=_bearer(token))
        assert me.json()["preferred_language"] == "ukr"
        assert me.json()["interface_language"] == "eng"

    async def test_a_submission_in_another_review_language_leaves_it_alone(
        self,
        portal_client: AsyncClient,
        portal_seed: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        file_task: uuid.UUID,
    ) -> None:
        """A real portal submission with ``response_language=eng``.

        The core writes the review language on it (``submission_core``);
        the interface language set before must come through unchanged.
        The storage and the queue are doubles, the database is live.
        """
        provisioned = await _provision(portal_client, "pete")
        student_id = uuid.UUID(str(provisioned["student_id"]))
        enrolled = await portal_client.post(
            "/api/v1/students/enrollments",
            json={
                "student_id": str(student_id),
                "course_node_id": str(portal_seed["root_id"]),
            },
        )
        assert enrolled.status_code == 201, enrolled.text
        token = await _login(portal_client, portal_seed, "pete")
        put = await portal_client.put(
            _INTERFACE_LANGUAGE, json={"language": "ukr"}, headers=_bearer(token)
        )
        assert put.status_code == 200, put.text

        s3 = AsyncMock()
        s3.upload_smart = AsyncMock(return_value=("s3://bucket/stored", 64))
        s3.delete_object = AsyncMock()
        arq = AsyncMock()
        arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq:hw:m3"))
        app.dependency_overrides[get_s3_client] = lambda: s3
        app.dependency_overrides[get_arq_redis] = lambda: arq

        submitted = await portal_client.post(
            f"/api/v1/portal/tasks/{file_task}/submissions",
            data={"response_language": "eng"},
            files={"file": ("solution.py", b"print('hi')\n", "text/x-python")},
            headers=_bearer(token),
        )
        assert submitted.status_code == 202, submitted.text

        assert await _stored_languages(session_factory, student_id) == ("ukr", "eng")


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
        student_id = str(provisioned["student_id"])
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
