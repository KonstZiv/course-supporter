"""Unit tests for the portal language lookup (step Г2 §1.1).

Pins three things the portal twin exists for: the student gets the SAME
list as the author, under their own key; the author route's lock is not
loosened to let them in; and neither door opens without a session.

``get_current_student`` / ``get_current_tenant`` are overridden per client
— the list itself is read from ``config/languages.yaml`` for real, so a
drift between the two routes would fail here rather than on the wire.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_student, get_current_tenant
from course_supporter.auth.context import StudentContext, TenantContext

_PORTAL_URL = "/api/v1/portal/languages"
_AUTHOR_URL = "/api/v1/config/languages"

STUB_STUDENT = StudentContext(
    student_id=uuid.uuid4(),
    tenant_id=uuid.uuid4(),
    login="alice",
    display_name="Alice",
)

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)


@pytest.fixture()
async def anon_client() -> AsyncClient:
    """No auth override at all — both doors must refuse."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac  # type: ignore[misc]


@pytest.fixture()
async def student_client() -> AsyncClient:
    app.dependency_overrides[get_current_student] = lambda: STUB_STUDENT
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestPortalLanguages:
    async def test_returns_the_whitelist(self, student_client: AsyncClient) -> None:
        resp = await student_client.get(_PORTAL_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == len(body["items"]) == 58
        codes = {item["code"] for item in body["items"]}
        assert {"ukr", "eng", "rus"} <= codes

    async def test_items_carry_code_and_english_name(
        self, student_client: AsyncClient
    ) -> None:
        for item in (await student_client.get(_PORTAL_URL)).json()["items"]:
            assert isinstance(item["code"], str) and len(item["code"]) == 3
            assert item["code"] == item["code"].lower()
            assert isinstance(item["name_en"], str) and item["name_en"]
            assert "name_native" in item  # optional value, mandatory key

    async def test_body_is_identical_to_the_author_route(
        self, student_client: AsyncClient
    ) -> None:
        """One list, two doors — a drift between them would be a silent bug.

        The portal twin exists because the author route's scope lock is not
        passable with a bearer token, NOT because the data differs. Comparing
        whole bodies is what keeps a future edit to one route from quietly
        giving the two surfaces different vocabularies.
        """
        portal = await student_client.get(_PORTAL_URL)
        app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
        author = await student_client.get(_AUTHOR_URL)
        assert author.status_code == portal.status_code == 200
        assert portal.json() == author.json()


class TestDoorsStayShut:
    async def test_portal_route_needs_a_session(self, anon_client: AsyncClient) -> None:
        resp = await anon_client.get(_PORTAL_URL)
        assert resp.status_code == 401

    async def test_author_route_still_refuses_a_bearer(
        self, anon_client: AsyncClient
    ) -> None:
        """Adding the portal twin must not loosen the author door.

        A student holds a bearer token and no API key, so the author route
        is unreachable for them — which is the reason the twin was added
        rather than the scope lock relaxed.
        """
        resp = await anon_client.get(
            _AUTHOR_URL, headers={"Authorization": "Bearer whatever"}
        )
        assert resp.status_code == 401
