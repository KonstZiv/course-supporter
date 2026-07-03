"""End-to-end integration: portal password-recovery + DD-6-J/DD-6-M (Phase 6 R2).

Uses the FastAPI app with overridden ``get_session`` (real committed session),
``get_current_tenant`` (stub PREP tenant), and ``get_arq_redis`` (a fake that
captures enqueued emails — the raw token lives only in the mailed link, so the
capture is how a test drives confirm / reset). ``get_current_student`` is NOT
overridden — the bearer flow is exercised for real. Cleanup runs in reverse FK
order at teardown.

Coverage:
* Full self-service flow: set recovery-email → confirm → forgot → reset → the
  new password logs in and the old one does not.
* Anti-enumeration: forgot on an unknown login OR a pending (unconfirmed)
  address → generic 202 with zero mail.
* Changing the address resets confirmed and burns the old confirm token.
* Single-use redemption; a revoked credential is rejected at the reset gate and
  never revived; a weak new password 422s without burning the token.
* DD-6-J author reset (204 / 422 / 404) and DD-6-M roster tenant_id.
* forgot rate-limit → 429.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    Student,
    StudentCredential,
    StudentCredentialToken,
    Tenant,
)

pytestmark = pytest.mark.requires_db

_PASSWORD = "correct horse 10"
_NEW_PASSWORD = "brand new pass 20"
_EMAIL = "alice@example.com"


class _FakeArq:
    """Captures ``enqueue_job`` calls so a test can read the mailed link."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append({"function": function, "kwargs": kwargs})
        return None

    def emails(self, message_id: str) -> list[dict[str, Any]]:
        return [
            c["kwargs"]
            for c in self.calls
            if c["kwargs"].get("message_id") == message_id
        ]

    def clear(self) -> None:
        self.calls.clear()


def _token_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query)["token"][0]


@pytest.fixture()
async def seed(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Seed a Tenant + one bare Student (no credential — DD-6-J 404 case)."""
    async with session_factory() as session:
        tenant = Tenant(name=f"recovery-test-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        bare = Student(tenant_id=tenant.id, external_id="bare-no-credential")
        session.add(bare)
        await session.flush()
        await session.commit()
        seed = {"tenant_id": tenant.id, "bare_student_id": bare.id}

    yield seed

    async with session_factory() as session:
        student_ids = (
            select(Student.id)
            .where(Student.tenant_id == seed["tenant_id"])
            .scalar_subquery()
        )
        cred_ids = (
            select(StudentCredential.id)
            .where(StudentCredential.tenant_id == seed["tenant_id"])
            .scalar_subquery()
        )
        await session.execute(
            StudentCredentialToken.__table__.delete().where(
                StudentCredentialToken.credential_id.in_(cred_ids)
            )
        )
        await session.execute(
            StudentCredential.__table__.delete().where(
                StudentCredential.tenant_id == seed["tenant_id"]
            )
        )
        await session.execute(
            Student.__table__.delete().where(Student.id.in_(student_ids))
        )
        await session.execute(
            Tenant.__table__.delete().where(Tenant.id == seed["tenant_id"])
        )
        await session.commit()


@pytest.fixture()
async def fake_arq() -> _FakeArq:
    return _FakeArq()


@pytest.fixture()
async def client(
    seed: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
    fake_arq: _FakeArq,
) -> AsyncGenerator[AsyncClient]:
    """FastAPI client: real committed session + stub PREP tenant + fake arq."""
    tenant_ctx = TenantContext(
        tenant_id=seed["tenant_id"],
        tenant_name="recovery-test",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_test",
    )

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_current_tenant] = lambda: tenant_ctx
    app.dependency_overrides[get_arq_redis] = lambda: fake_arq

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http

    app.dependency_overrides.clear()


# --- helpers -----------------------------------------------------------------


async def _provision(client: AsyncClient, login: str, password: str = _PASSWORD) -> str:
    resp = await client.post(
        "/api/v1/students",
        json={"mode": "generate", "login": login, "password": password},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["student_id"])


async def _login(
    client: AsyncClient, tenant_id: uuid.UUID, login: str, password: str
) -> int:
    resp = await client.post(
        "/api/v1/portal/login",
        json={"tenant_id": str(tenant_id), "login": login, "password": password},
    )
    return resp.status_code


async def _token(
    client: AsyncClient, tenant_id: uuid.UUID, login: str, password: str = _PASSWORD
) -> str:
    resp = await client.post(
        "/api/v1/portal/login",
        json={"tenant_id": str(tenant_id), "login": login, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _credential_is_active(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> bool:
    async with session_factory() as session:
        row = await session.execute(
            select(StudentCredential.is_active).where(
                StudentCredential.tenant_id == tenant_id
            )
        )
        return bool(row.scalar_one())


# --- tests -------------------------------------------------------------------


class TestFullFlow:
    async def test_set_confirm_forgot_reset(
        self,
        client: AsyncClient,
        seed: dict[str, uuid.UUID],
        fake_arq: _FakeArq,
    ) -> None:
        tenant_id = seed["tenant_id"]
        await _provision(client, "alice")
        token = await _token(client, tenant_id, "alice")

        # set recovery email → 200, pending
        setr = await client.post(
            "/api/v1/portal/recovery-email",
            json={"email": _EMAIL},
            headers=_auth(token),
        )
        assert setr.status_code == 200, setr.text
        assert setr.json()["recovery_email"] == _EMAIL
        assert setr.json()["recovery_email_confirmed"] is False

        # a confirm mail was enqueued to that address
        confirm_mails = fake_arq.emails("email_confirm")
        assert len(confirm_mails) == 1
        assert confirm_mails[0]["to"] == _EMAIL
        confirm_token = _token_from_url(confirm_mails[0]["context"]["confirm_url"])

        # /me shows the pending address
        me = await client.get("/api/v1/portal/me", headers=_auth(token))
        assert me.json()["recovery_email"] == _EMAIL
        assert me.json()["recovery_email_confirmed"] is False

        # confirm (public) → 204, then /me confirmed
        conf = await client.post(
            "/api/v1/portal/recovery-email/confirm",
            json={"token": confirm_token},
        )
        assert conf.status_code == 204, conf.text
        me2 = await client.get("/api/v1/portal/me", headers=_auth(token))
        assert me2.json()["recovery_email_confirmed"] is True

        # forgot → 202 + reset mail
        fake_arq.clear()
        forgot = await client.post(
            "/api/v1/portal/password/forgot",
            json={"tenant_id": str(tenant_id), "login": "alice"},
        )
        assert forgot.status_code == 202, forgot.text
        reset_mails = fake_arq.emails("password_reset")
        assert len(reset_mails) == 1
        assert reset_mails[0]["to"] == _EMAIL
        reset_token = _token_from_url(reset_mails[0]["context"]["reset_url"])

        # reset → 204, new password logs in, old does not
        reset = await client.post(
            "/api/v1/portal/password/reset",
            json={"token": reset_token, "password": _NEW_PASSWORD},
        )
        assert reset.status_code == 204, reset.text
        assert await _login(client, tenant_id, "alice", _PASSWORD) == 401
        assert await _login(client, tenant_id, "alice", _NEW_PASSWORD) == 200


class TestAntiEnumeration:
    async def test_forgot_unknown_login_202_no_mail(
        self, client: AsyncClient, seed: dict[str, uuid.UUID], fake_arq: _FakeArq
    ) -> None:
        resp = await client.post(
            "/api/v1/portal/password/forgot",
            json={"tenant_id": str(seed["tenant_id"]), "login": "ghost"},
        )
        assert resp.status_code == 202
        assert fake_arq.emails("password_reset") == []

    async def test_forgot_pending_email_202_no_mail(
        self, client: AsyncClient, seed: dict[str, uuid.UUID], fake_arq: _FakeArq
    ) -> None:
        tenant_id = seed["tenant_id"]
        await _provision(client, "pend")
        token = await _token(client, tenant_id, "pend")
        # set but do NOT confirm
        await client.post(
            "/api/v1/portal/recovery-email",
            json={"email": "pend@example.com"},
            headers=_auth(token),
        )
        fake_arq.clear()  # drop the confirm mail
        forgot = await client.post(
            "/api/v1/portal/password/forgot",
            json={"tenant_id": str(tenant_id), "login": "pend"},
        )
        assert forgot.status_code == 202
        assert fake_arq.emails("password_reset") == []  # unverified → no reset mail


class TestChangeEmail:
    async def test_change_resets_confirmed_and_burns_old_token(
        self, client: AsyncClient, seed: dict[str, uuid.UUID], fake_arq: _FakeArq
    ) -> None:
        tenant_id = seed["tenant_id"]
        await _provision(client, "chg")
        token = await _token(client, tenant_id, "chg")

        # set A + confirm
        await client.post(
            "/api/v1/portal/recovery-email",
            json={"email": "a@example.com"},
            headers=_auth(token),
        )
        token_a = _token_from_url(
            fake_arq.emails("email_confirm")[0]["context"]["confirm_url"]
        )
        assert (
            await client.post(
                "/api/v1/portal/recovery-email/confirm", json={"token": token_a}
            )
        ).status_code == 204
        me = await client.get("/api/v1/portal/me", headers=_auth(token))
        assert me.json()["recovery_email_confirmed"] is True

        # change to B → confirmed reset
        fake_arq.clear()
        setb = await client.post(
            "/api/v1/portal/recovery-email",
            json={"email": "b@example.com"},
            headers=_auth(token),
        )
        assert setb.status_code == 200
        me2 = await client.get("/api/v1/portal/me", headers=_auth(token))
        assert me2.json()["recovery_email"] == "b@example.com"
        assert me2.json()["recovery_email_confirmed"] is False

        # the old confirm token is now burned → 400
        reuse = await client.post(
            "/api/v1/portal/recovery-email/confirm", json={"token": token_a}
        )
        assert reuse.status_code == 400


class TestRedemptionGate:
    async def _to_reset_token(
        self, client: AsyncClient, tenant_id: uuid.UUID, login: str, fake_arq: _FakeArq
    ) -> str:
        token = await _token(client, tenant_id, login)
        await client.post(
            "/api/v1/portal/recovery-email",
            json={"email": f"{login}@example.com"},
            headers=_auth(token),
        )
        ct = _token_from_url(
            fake_arq.emails("email_confirm")[0]["context"]["confirm_url"]
        )
        await client.post("/api/v1/portal/recovery-email/confirm", json={"token": ct})
        fake_arq.clear()
        await client.post(
            "/api/v1/portal/password/forgot",
            json={"tenant_id": str(tenant_id), "login": login},
        )
        return _token_from_url(
            fake_arq.emails("password_reset")[0]["context"]["reset_url"]
        )

    async def test_reset_token_single_use(
        self, client: AsyncClient, seed: dict[str, uuid.UUID], fake_arq: _FakeArq
    ) -> None:
        tenant_id = seed["tenant_id"]
        await _provision(client, "once")
        rt = await self._to_reset_token(client, tenant_id, "once", fake_arq)
        first = await client.post(
            "/api/v1/portal/password/reset",
            json={"token": rt, "password": _NEW_PASSWORD},
        )
        assert first.status_code == 204
        second = await client.post(
            "/api/v1/portal/password/reset",
            json={"token": rt, "password": "another one 30"},
        )
        assert second.status_code == 400

    async def test_reset_on_revoked_rejected_and_not_revived(
        self,
        client: AsyncClient,
        seed: dict[str, uuid.UUID],
        fake_arq: _FakeArq,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant_id = seed["tenant_id"]
        student_id = await _provision(client, "revk")
        rt = await self._to_reset_token(client, tenant_id, "revk", fake_arq)

        # revoke access, THEN try to reset
        assert (
            await client.post(f"/api/v1/students/{student_id}/revoke")
        ).status_code == 204
        reset = await client.post(
            "/api/v1/portal/password/reset",
            json={"token": rt, "password": _NEW_PASSWORD},
        )
        assert reset.status_code == 400  # redemption gate

        # access was NOT revived, and the password was NOT changed
        assert await _credential_is_active(session_factory, tenant_id) is False
        assert (
            await client.post(f"/api/v1/students/{student_id}/restore")
        ).status_code == 204
        assert await _login(client, tenant_id, "revk", _NEW_PASSWORD) == 401
        assert await _login(client, tenant_id, "revk", _PASSWORD) == 200

    async def test_reset_weak_password_422_token_not_burned(
        self, client: AsyncClient, seed: dict[str, uuid.UUID], fake_arq: _FakeArq
    ) -> None:
        tenant_id = seed["tenant_id"]
        await _provision(client, "weak")
        rt = await self._to_reset_token(client, tenant_id, "weak", fake_arq)
        weak = await client.post(
            "/api/v1/portal/password/reset",
            json={"token": rt, "password": "short"},
        )
        assert weak.status_code == 422
        # the token survived the validation failure → a valid reset still works
        ok = await client.post(
            "/api/v1/portal/password/reset",
            json={"token": rt, "password": _NEW_PASSWORD},
        )
        assert ok.status_code == 204


class TestInvalidTokens:
    async def test_confirm_unknown_token_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/portal/recovery-email/confirm", json={"token": "nope"}
        )
        assert resp.status_code == 400

    async def test_reset_unknown_token_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/portal/password/reset",
            json={"token": "nope", "password": _NEW_PASSWORD},
        )
        assert resp.status_code == 400


class TestAuthorReset:
    async def test_admin_reset_changes_password(
        self, client: AsyncClient, seed: dict[str, uuid.UUID]
    ) -> None:
        tenant_id = seed["tenant_id"]
        student_id = await _provision(client, "adm")
        assert await _login(client, tenant_id, "adm", _PASSWORD) == 200
        resp = await client.post(
            f"/api/v1/students/{student_id}/password",
            json={"password": _NEW_PASSWORD},
        )
        assert resp.status_code == 204, resp.text
        assert await _login(client, tenant_id, "adm", _PASSWORD) == 401
        assert await _login(client, tenant_id, "adm", _NEW_PASSWORD) == 200

    async def test_admin_reset_weak_422(
        self, client: AsyncClient, seed: dict[str, uuid.UUID]
    ) -> None:
        student_id = await _provision(client, "admw")
        resp = await client.post(
            f"/api/v1/students/{student_id}/password", json={"password": "short"}
        )
        assert resp.status_code == 422

    async def test_admin_reset_unknown_student_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"/api/v1/students/{uuid.uuid4()}/password",
            json={"password": _NEW_PASSWORD},
        )
        assert resp.status_code == 404

    async def test_admin_reset_no_credential_404(
        self, client: AsyncClient, seed: dict[str, uuid.UUID]
    ) -> None:
        resp = await client.post(
            f"/api/v1/students/{seed['bare_student_id']}/password",
            json={"password": _NEW_PASSWORD},
        )
        assert resp.status_code == 404


class TestRosterTenantId:
    async def test_roster_includes_tenant_id(
        self, client: AsyncClient, seed: dict[str, uuid.UUID]
    ) -> None:
        resp = await client.get("/api/v1/students")
        assert resp.status_code == 200, resp.text
        assert resp.json()["tenant_id"] == str(seed["tenant_id"])


class TestForgotRateLimit:
    async def test_forgot_rate_limited(
        self, client: AsyncClient, seed: dict[str, uuid.UUID]
    ) -> None:
        tenant_id = seed["tenant_id"]
        body = {"tenant_id": str(tenant_id), "login": "rl-unique-login"}
        for _ in range(3):
            assert (
                await client.post("/api/v1/portal/password/forgot", json=body)
            ).status_code == 202
        assert (
            await client.post("/api/v1/portal/password/forgot", json=body)
        ).status_code == 429
