"""What the open-course tests (O-1) share: a world of two authors, an app
client over it, and the steps of enrollment by a link.

The world: author A with two courses (one with a child node) and author B with
one course. The client authenticates as A's ``PREP`` key; the ARQ pool is a
fake that keeps the emails (the confirm token lives only in the mailed link);
the rate limiter is the test's — none by default.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_arq_redis,
    get_current_tenant,
    get_rate_limiter,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.rate_limits import RedisRateLimiter
from course_supporter.auth.student_passwords import hash_password
from course_supporter.config import RateLimit, get_settings
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    CourseNode,
    Student,
    StudentCredential,
    Tenant,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node

PASSWORD = "correct horse 10"
TEXT_SHA = "a" * 64
SIGNUP_EMAIL = "portal_signup_confirm"


def terms() -> str:
    """The terms revision in force."""
    return get_settings().billing_terms_version


class FakeArq:
    """Keeps ``enqueue_job`` calls so a test reads the mailed links."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append({"function": function, "kwargs": kwargs})

    def emails(self, message_id: str = SIGNUP_EMAIL) -> list[dict[str, Any]]:
        return [
            c["kwargs"]
            for c in self.calls
            if c["kwargs"].get("message_id") == message_id
        ]

    def last_token(self, message_id: str = SIGNUP_EMAIL) -> str:
        """The token of the newest mailed link of ``message_id``."""
        context = self.emails(message_id)[-1]["context"]
        url: str = context.get("confirm_url") or context["reset_url"]
        return parse_qs(urlparse(url).query)["token"][0]


class NoLimits(RedisRateLimiter):
    """A limiter that counts nothing (tests not about limits)."""

    def __init__(self) -> None:
        super().__init__(None)

    async def hit(self, name: str, subject: str, rule: RateLimit) -> None:
        return None


@dataclass(frozen=True)
class World:
    tenant_a: uuid.UUID
    tenant_a_name: str
    course: uuid.UUID
    """A's course in Ukrainian, with a child node."""
    other_course: uuid.UUID
    """A's second course, in English."""
    child: uuid.UUID
    tenant_b: uuid.UUID
    tenant_b_name: str
    course_b: uuid.UUID


async def make_world(session_factory: async_sessionmaker[AsyncSession]) -> World:
    tag = uuid.uuid4().hex[:8]
    async with session_factory() as session:
        tenant_a = Tenant(name=f"o1-author-a-{tag}")
        tenant_b = Tenant(name=f"o1-author-b-{tag}")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        course = make_root_course_node(
            tenant_id=tenant_a.id, title="Python basics", order=0
        )
        other = make_root_course_node(
            tenant_id=tenant_a.id, title="Second", order=1, default_language="eng"
        )
        course_b = make_root_course_node(tenant_id=tenant_b.id, title="B", order=0)
        session.add_all([course, other, course_b])
        await session.flush()
        child = CourseNode(tenant_id=tenant_a.id, parent_id=course.id, title="L1")
        session.add(child)
        await session.commit()
        return World(
            tenant_a=tenant_a.id,
            tenant_a_name=tenant_a.name,
            course=course.id,
            other_course=other.id,
            child=child.id,
            tenant_b=tenant_b.id,
            tenant_b_name=tenant_b.name,
            course_b=course_b.id,
        )


async def drop_world(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Billing rows first (they point at students), then the tenants — their
    courses, students, logins, enrollments and signups go with them."""
    for tenant_id in (world.tenant_a, world.tenant_b):
        await drop_billing_of_tenant(session_factory, tenant_id)
    async with session_factory() as session:
        await session.execute(
            delete(Tenant).where(Tenant.id.in_([world.tenant_a, world.tenant_b]))
        )
        await session.commit()


@asynccontextmanager
async def app_client(
    world: World,
    session_factory: async_sessionmaker[AsyncSession],
    arq: FakeArq,
    limiter: RedisRateLimiter | None = None,
) -> AsyncIterator[AsyncClient]:
    """The app as author A's PREP key; a real committed session per request."""
    tenant = TenantContext(
        tenant_id=world.tenant_a,
        tenant_name=world.tenant_a_name,
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_test",
    )

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = lambda: tenant
    app.dependency_overrides[get_arq_redis] = lambda: arq
    app.dependency_overrides[get_rate_limiter] = lambda: limiter or NoLimits()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def open_link(client: AsyncClient, course_id: uuid.UUID) -> str:
    """Open a course's link as the author; its key."""
    resp = await client.post(f"/api/v1/nodes/{course_id}/enrollment-link/open")
    assert resp.status_code == 200, resp.text
    key: str = resp.json()["key"]
    return key


def signup_body(login: str, email: str, **override: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "login": login,
        "password": PASSWORD,
        "email": email,
        "adult": True,
        "terms_version": terms(),
        "text_sha256": TEXT_SHA,
    }
    body.update(override)
    return body


async def signup(
    client: AsyncClient,
    tenant_id: uuid.UUID,
    key: str,
    login: str,
    email: str,
    **override: Any,
) -> Response:
    return await client.post(
        f"/api/v1/portal/join/{tenant_id}/{key}/signup",
        json=signup_body(login, email, **override),
    )


async def confirm(client: AsyncClient, token: str) -> Response:
    return await client.post("/api/v1/portal/join/confirm", json={"token": token})


async def enroll_by_link(
    client: AsyncClient,
    arq: FakeArq,
    tenant_id: uuid.UUID,
    key: str,
    login: str,
    email: str,
    **override: Any,
) -> dict[str, Any]:
    """Sign up and confirm; the confirm's answer."""
    resp = await signup(client, tenant_id, key, login, email, **override)
    assert resp.status_code == 202, resp.text
    done = await confirm(client, arq.last_token())
    assert done.status_code == 200, done.text
    body: dict[str, Any] = done.json()
    return body


async def provision(client: AsyncClient, login: str, password: str = PASSWORD) -> str:
    """A student of author A with a login, as the author makes one."""
    resp = await client.post(
        "/api/v1/students",
        json={"mode": "generate", "login": login, "password": password},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["student_id"])


async def portal_token(
    client: AsyncClient, tenant_id: uuid.UUID, login: str, password: str = PASSWORD
) -> str:
    resp = await client.post(
        "/api/v1/portal/login",
        json={"tenant_id": str(tenant_id), "login": login, "password": password},
    )
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["access_token"]
    return token


async def add_login(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    login: str,
    *,
    email: str | None,
    password: str = PASSWORD,
    confirmed: bool = True,
    active: bool = True,
) -> uuid.UUID:
    """A student with a login straight in the database; its student id."""
    async with session_factory() as session:
        student = Student(tenant_id=tenant_id, external_id=f"portal-{uuid.uuid4().hex}")
        session.add(student)
        await session.flush()
        session.add(
            StudentCredential(
                student_id=student.id,
                tenant_id=tenant_id,
                login=login,
                password_hash=hash_password(password),
                is_active=active,
                recovery_email=email,
                recovery_email_confirmed_at=(
                    datetime.now(UTC) if confirmed and email else None
                ),
            )
        )
        await session.commit()
        return student.id
