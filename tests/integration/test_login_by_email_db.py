"""Login by a confirmed email (O-1, variant A) on a live database.

Locks of the task:

* (е) one match — a session; two authors with one email and different
  passwords — the one whose password matched; the same password at both —
  ``choose``; an unconfirmed email — 401; a deactivated login — 401; the 401
  is the same for an unknown email and a wrong password.
* (ї) one email on two logins of ONE author (a family, a shared mailbox):
  different passwords — the matched one; the same — ``choose`` with two logins
  of that author; the choice by tenant_id + login enters exactly that one;
  a signup with an email already in the tenant — 202, the request made.

Requires ``docker compose up -d`` (PostgreSQL), migrated to head; ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.storage.orm import PortalSignup
from tests._helpers.open_courses import (
    PASSWORD,
    FakeArq,
    World,
    add_login,
    app_client,
    auth,
    drop_world,
    make_world,
    open_link,
    signup,
)

pytestmark = pytest.mark.requires_db

EMAIL = "Shared@Example.com"
OTHER_PASSWORD = "another pass 22"


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[World]:
    made = await make_world(session_factory)
    yield made
    await drop_world(session_factory, made)


@pytest.fixture()
async def client(
    world: World, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncClient]:
    async with app_client(world, session_factory, FakeArq()) as http:
        yield http


async def _login(
    client: AsyncClient, password: str = PASSWORD, **more: Any
) -> Response:
    return await client.post(
        "/api/v1/portal/login-by-email",
        json={"email": EMAIL.lower(), "password": password, **more},
    )


async def _me(client: AsyncClient, resp: Response) -> dict[str, Any]:
    me = await client.get(
        "/api/v1/portal/me", headers=auth(resp.json()["access_token"])
    )
    assert me.status_code == 200, me.text
    body: dict[str, Any] = me.json()
    return body


class TestAcrossAuthors:
    async def test_one_match_is_a_session(
        self,
        client: AsyncClient,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        student = await add_login(session_factory, world.tenant_a, "solo", email=EMAIL)
        resp = await _login(client)
        assert resp.status_code == 200, resp.text
        assert resp.json()["student_id"] == str(student)
        assert resp.json()["tenant_id"] == str(world.tenant_a)
        assert (await _me(client, resp))["login"] == "solo"

    async def test_two_authors_different_passwords_enter_the_matched(
        self,
        client: AsyncClient,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_login(session_factory, world.tenant_a, "at-a", email=EMAIL)
        at_b = await add_login(
            session_factory,
            world.tenant_b,
            "at-b",
            email=EMAIL,
            password=OTHER_PASSWORD,
        )
        resp = await _login(client, OTHER_PASSWORD)
        assert resp.status_code == 200, resp.text
        assert resp.json()["student_id"] == str(at_b)
        assert resp.json()["tenant_id"] == str(world.tenant_b)

    async def test_two_authors_same_password_choose_then_enter_the_chosen(
        self,
        client: AsyncClient,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_login(session_factory, world.tenant_a, "at-a", email=EMAIL)
        at_b = await add_login(session_factory, world.tenant_b, "at-b", email=EMAIL)
        choose = await _login(client)
        assert choose.status_code == 200
        assert "access_token" not in choose.json()
        assert sorted(
            (c["tenant_id"], c["tenant_name"], c["login"])
            for c in choose.json()["choose"]
        ) == sorted(
            [
                (str(world.tenant_a), world.tenant_a_name, "at-a"),
                (str(world.tenant_b), world.tenant_b_name, "at-b"),
            ]
        )
        chosen = await _login(client, tenant_id=str(world.tenant_b), login="at-b")
        assert chosen.json()["student_id"] == str(at_b)
        wrong = await _login(
            client, OTHER_PASSWORD, tenant_id=str(world.tenant_b), login="at-b"
        )
        assert wrong.status_code == 401

    async def test_choose_lists_only_the_matched(
        self,
        client: AsyncClient,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_login(session_factory, world.tenant_a, "a1", email=EMAIL)
        await add_login(session_factory, world.tenant_a, "a2", email=EMAIL)
        await add_login(
            session_factory, world.tenant_b, "b1", email=EMAIL, password=OTHER_PASSWORD
        )
        choose = await _login(client)
        assert [c["login"] for c in choose.json()["choose"]] == ["a1", "a2"]


class TestRefused:
    async def test_unconfirmed_deactivated_unknown_and_wrong_are_one_401(
        self,
        client: AsyncClient,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_login(
            session_factory, world.tenant_a, "pending", email=EMAIL, confirmed=False
        )
        answers = [await _login(client)]
        await add_login(
            session_factory, world.tenant_b, "off", email=EMAIL, active=False
        )
        answers.append(await _login(client))
        answers.append(
            await client.post(
                "/api/v1/portal/login-by-email",
                json={"email": "nobody@example.com", "password": PASSWORD},
            )
        )
        await add_login(session_factory, world.tenant_b, "on", email=EMAIL)
        answers.append(await _login(client, "wrong password 1"))
        login_401 = await client.post(
            "/api/v1/portal/login",
            json={"tenant_id": str(world.tenant_a), "login": "x", "password": "y"},
        )
        assert {a.status_code for a in answers} == {401}
        assert {a.text for a in answers} == {login_401.text}


class TestOneAuthorOneMailbox:
    async def test_different_passwords_enter_the_matched(
        self,
        client: AsyncClient,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_login(session_factory, world.tenant_a, "mum", email=EMAIL)
        kid = await add_login(
            session_factory, world.tenant_a, "kid", email=EMAIL, password=OTHER_PASSWORD
        )
        resp = await _login(client, OTHER_PASSWORD)
        assert resp.json()["student_id"] == str(kid)

    async def test_same_password_choose_two_logins_then_the_chosen(
        self,
        client: AsyncClient,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_login(session_factory, world.tenant_a, "twin-1", email=EMAIL)
        twin_2 = await add_login(session_factory, world.tenant_a, "twin-2", email=EMAIL)
        choose = await _login(client)
        rows = choose.json()["choose"]
        assert [(r["tenant_id"], r["login"]) for r in rows] == [
            (str(world.tenant_a), "twin-1"),
            (str(world.tenant_a), "twin-2"),
        ]
        chosen = await _login(client, tenant_id=str(world.tenant_a), login="twin-2")
        assert chosen.json()["student_id"] == str(twin_2)
        assert (await _me(client, chosen))["login"] == "twin-2"

    async def test_a_signup_with_an_email_already_in_the_tenant(
        self,
        client: AsyncClient,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_login(session_factory, world.tenant_a, "parent", email=EMAIL)
        key = await open_link(client, world.course)
        resp = await signup(client, world.tenant_a, key, "child", EMAIL)
        assert resp.status_code == 202, resp.text
        async with session_factory() as session:
            made = await session.scalar(
                select(func.count())
                .select_from(PortalSignup)
                .where(PortalSignup.id == uuid.UUID(resp.json()["signup_id"]))
            )
        assert made == 1
