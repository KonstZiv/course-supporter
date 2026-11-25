"""Open courses (O-1): rate limits in Redis, privacy, and the cleanup task.

Locks of the task:

* (є) over a limit — 429 ``rate_limited`` with ``Retry-After`` and no numbers
  of the limit in the answer, counted in Redis (the tests' own database);
  without Redis the request passes and a WARNING is logged.
* (і) privacy: signup, resend, confirm, login by email (success and refusal),
  joining a course and the cleanup run with a marker in the email, the login
  and the password — the marker is in no log line and in no 4xx body.
* (ж) the cleanup deletes the expired requests and the confirmed ones older
  than 30 days, and keeps the fresh ones.

Requires ``docker compose up -d`` (PostgreSQL, Redis); ``--run-db --run-redis``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from arq.connections import ArqRedis
from httpx import AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.auth.rate_limits import KEY_PREFIX, RedisRateLimiter
from course_supporter.auth.recovery_tokens import hash_token
from course_supporter.config import RateLimit, get_settings
from course_supporter.open_courses.cleanup import PORTAL_SIGNUPS_CLEANUP
from course_supporter.periodic.runner import run_task
from course_supporter.storage.orm import PortalSignup
from tests._helpers.open_courses import (
    PASSWORD,
    TEXT_SHA,
    FakeArq,
    World,
    app_client,
    auth,
    confirm,
    drop_world,
    make_world,
    open_link,
    portal_token,
    provision,
    signup,
    terms,
)

pytestmark = pytest.mark.requires_db


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[World]:
    made = await make_world(session_factory)
    yield made
    await drop_world(session_factory, made)


def _tight(monkeypatch: pytest.MonkeyPatch, name: str, limit: int = 1) -> None:
    monkeypatch.setattr(get_settings(), name, RateLimit(limit=limit, window_s=60))


def _assert_limited(resp: Response) -> None:
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"]["code"] == "rate_limited"
    assert 1 <= int(resp.headers["Retry-After"]) <= 60
    assert not any(ch.isdigit() for ch in resp.json()["detail"]["details"])


# ── (є) Rate limits ─────────────────────────────────────────────────────────


@pytest.mark.requires_redis
class TestRateLimits:
    @pytest.fixture()
    async def client(
        self,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
        arq_redis: ArqRedis,
    ) -> AsyncIterator[AsyncClient]:
        limiter = RedisRateLimiter(arq_redis)
        async with app_client(world, session_factory, FakeArq(), limiter) as http:
            yield http

    async def test_signup_per_ip(
        self,
        client: AsyncClient,
        world: World,
        arq_redis: ArqRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _tight(monkeypatch, "rate_limit_join_signup_ip")
        key = await open_link(client, world.course)
        assert (
            await signup(client, world.tenant_a, key, "one", "1@x.io")
        ).status_code == 202
        _assert_limited(await signup(client, world.tenant_a, key, "two", "2@x.io"))
        stored = [k.decode() for k in await arq_redis.keys(f"{KEY_PREFIX}*")]
        assert stored and all("127.0.0.1" not in k for k in stored)

    async def test_signup_per_course_key(
        self, client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tight(monkeypatch, "rate_limit_join_signup_key")
        key = await open_link(client, world.course)
        assert (
            await signup(client, world.tenant_a, key, "one", "1@x.io")
        ).status_code == 202
        _assert_limited(await signup(client, world.tenant_a, key, "two", "2@x.io"))
        other = await open_link(client, world.other_course)
        assert (
            await signup(client, world.tenant_a, other, "three", "3@x.io")
        ).status_code == 202

    async def test_resend_once_a_minute(
        self, client: AsyncClient, world: World
    ) -> None:
        key = await open_link(client, world.course)
        made = await signup(client, world.tenant_a, key, "one", "1@x.io")
        body = {"signup_id": made.json()["signup_id"]}
        assert (
            await client.post("/api/v1/portal/join/resend", json=body)
        ).status_code == 202
        _assert_limited(await client.post("/api/v1/portal/join/resend", json=body))

    async def test_login_by_email_per_email(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tight(monkeypatch, "rate_limit_login_by_email_email")
        body = {"email": "who@x.io", "password": "whatever pass"}
        first = await client.post("/api/v1/portal/login-by-email", json=body)
        assert first.status_code == 401
        _assert_limited(await client.post("/api/v1/portal/login-by-email", json=body))

    async def test_course_join_per_student(
        self, client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tight(monkeypatch, "rate_limit_join_course_student")
        key = await open_link(client, world.course)
        await provision(client, "busy")
        token = await portal_token(client, world.tenant_a, "busy")
        body = {
            "tenant_id": str(world.tenant_a),
            "key": key,
            "adult": True,
            "terms_version": terms(),
            "text_sha256": TEXT_SHA,
        }
        url = "/api/v1/portal/courses/join"
        first = await client.post(url, json=body, headers=auth(token))
        assert first.status_code == 409  # no confirmed email — but counted
        _assert_limited(await client.post(url, json=body, headers=auth(token)))


class TestWithoutRedis:
    async def test_the_request_passes_with_a_warning(
        self,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _tight(monkeypatch, "rate_limit_join_signup_ip")
        down = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
        try:
            async with app_client(
                world, session_factory, FakeArq(), RedisRateLimiter(down)
            ) as client:
                key = await open_link(client, world.course)
                with capture_logs() as logs:
                    answers = [
                        await signup(client, world.tenant_a, key, f"u{n}", f"{n}@x.io")
                        for n in range(3)
                    ]
        finally:
            await down.aclose()
        assert [a.status_code for a in answers] == [202, 202, 202]
        warnings = [e for e in logs if e["event"] == "rate_limit_unavailable"]
        assert warnings and {w["log_level"] for w in warnings} == {"warning"}


# ── (і) Privacy ─────────────────────────────────────────────────────────────

MARK = "zq7mark"
MARK_EMAIL = f"{MARK}.user@{MARK}-mail.example"
MARK_LOGIN = f"{MARK}-login"
MARK_PASSWORD = f"{MARK}-password-123"


class TestPrivacy:
    async def test_no_email_login_token_or_password_in_logs_or_4xx(
        self,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        arq = FakeArq()
        refused: list[Response] = []
        async with app_client(world, session_factory, arq) as client:
            key = await open_link(client, world.course)
            other_key = await open_link(client, world.other_course)
            join_url = f"/api/v1/portal/join/{world.tenant_a}/{key}/signup"
            with capture_logs() as logs:
                made = await signup(
                    client,
                    world.tenant_a,
                    key,
                    MARK_LOGIN,
                    MARK_EMAIL,
                    password=MARK_PASSWORD,
                )
                assert made.status_code == 202, made.text
                refused.append(
                    await signup(client, world.tenant_a, key, MARK_LOGIN, MARK_EMAIL)
                )
                refused.append(
                    await client.post(
                        join_url,
                        json={
                            "login": f"{MARK}-x",
                            "password": MARK_PASSWORD,
                            "email": f"{MARK}-no-at",
                            "adult": True,
                            "terms_version": terms(),
                            "text_sha256": TEXT_SHA,
                        },
                    )
                )
                resent = await client.post(
                    "/api/v1/portal/join/resend",
                    json={"signup_id": made.json()["signup_id"]},
                )
                assert resent.status_code == 202
                token = arq.last_token()
                refused.append(await confirm(client, f"{MARK}-token"))
                refused.append(
                    await client.post(
                        f"/api/v1/portal/join/{made.json()['signup_id']}/login",
                        json={"login": f"{MARK}-other", "token": f"{MARK}-token"},
                    )
                )
                done = await confirm(client, token)
                assert done.status_code == 200, done.text
                session_token = done.json()["access_token"]

                ok = await client.post(
                    "/api/v1/portal/login-by-email",
                    json={"email": MARK_EMAIL, "password": MARK_PASSWORD},
                )
                assert ok.status_code == 200, ok.text
                refused.append(
                    await client.post(
                        "/api/v1/portal/login-by-email",
                        json={"email": MARK_EMAIL, "password": f"{MARK}-wrong-pass"},
                    )
                )
                refused.append(
                    await client.post(
                        "/api/v1/portal/login-by-email",
                        json={
                            "email": MARK_EMAIL,
                            "password": MARK_PASSWORD,
                            "tenant_id": str(world.tenant_a),
                            "login": f"{MARK}-nope",
                        },
                    )
                )
                joined = await client.post(
                    "/api/v1/portal/courses/join",
                    json={
                        "tenant_id": str(world.tenant_a),
                        "key": other_key,
                        "adult": True,
                        "terms_version": terms(),
                        "text_sha256": TEXT_SHA,
                    },
                    headers=auth(session_token),
                )
                assert joined.status_code == 201, joined.text
                refused.append(
                    await client.post(
                        "/api/v1/portal/courses/join",
                        json={
                            "tenant_id": str(world.tenant_a),
                            "key": f"{MARK}-key",
                            "adult": True,
                            "terms_version": terms(),
                            "text_sha256": TEXT_SHA,
                        },
                        headers=auth(session_token),
                    )
                )
                await run_task(PORTAL_SIGNUPS_CLEANUP, session_factory)

        async with session_factory() as session:
            stored_hash = await session.scalar(
                select(PortalSignup.password_hash).where(
                    PortalSignup.id == uuid.UUID(made.json()["signup_id"])
                )
            )
        assert [r.status_code for r in refused] == [409, 422, 404, 404, 401, 401, 404]
        for resp in refused:
            assert MARK not in resp.text, resp.text
        logged = repr(logs)
        assert logs
        for secret in (MARK, token, hash_token(token), stored_hash, PASSWORD):
            assert secret and secret not in logged
        masked = [e for e in logs if e.get("email_masked")]
        assert masked and {e["email_masked"] for e in masked} == {"z***@z***.example"}


# ── (ж) Cleanup ─────────────────────────────────────────────────────────────


class TestCleanup:
    async def test_expired_and_old_confirmed_go_fresh_stay(
        self,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)

        def row(login: str, created_days_ago: int, *, confirmed: bool) -> PortalSignup:
            created = now - timedelta(days=created_days_ago)
            return PortalSignup(
                tenant_id=world.tenant_a,
                course_node_id=world.course,
                login=login,
                password_hash="argon2-placeholder",
                email=f"{login}@x.io",
                adult_confirmed_at=created,
                terms_version=terms(),
                text_sha256=TEXT_SHA,
                token_hash=hash_token(uuid.uuid4().hex),
                created_at=created,
                expires_at=created + timedelta(days=7),
                confirmed_at=created + timedelta(hours=1) if confirmed else None,
            )

        rows = {
            "expired": row("expired", 8, confirmed=False),
            "fresh": row("fresh", 6, confirmed=False),
            "old-confirmed": row("old-confirmed", 31, confirmed=True),
            "new-confirmed": row("new-confirmed", 29, confirmed=True),
        }
        async with session_factory() as session:
            session.add_all(rows.values())
            await session.commit()
            ids = {name: r.id for name, r in rows.items()}

        with capture_logs() as logs:
            run = await run_task(PORTAL_SIGNUPS_CLEANUP, session_factory, now=now)
        assert run.error is None
        [line] = [e for e in logs if e["event"] == "periodic_task_run"]
        assert line["task"] == "portal_signups_cleanup"
        assert line["expired"] >= 1 and line["confirmed"] >= 1
        assert line["processed"] == line["expired"] + line["confirmed"]

        async with session_factory() as session:
            left = set(
                (
                    await session.scalars(
                        select(PortalSignup.id).where(PortalSignup.id.in_(ids.values()))
                    )
                ).all()
            )
        assert left == {ids["fresh"], ids["new-confirmed"]}
