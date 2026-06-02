"""Tests for ``GET /api/v1/config/languages`` (Task 2.4.13)."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)


@pytest.fixture()
async def client() -> AsyncClient:
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestGetAllowedLanguages:
    async def test_returns_57_items(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/config/languages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 57
        assert len(body["items"]) == 57

    async def test_each_item_has_iso_639_3_and_english_name(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/v1/config/languages")
        for item in resp.json()["items"]:
            assert isinstance(item["code"], str) and len(item["code"]) == 3
            assert item["code"] == item["code"].lower()
            assert isinstance(item["name_en"], str) and item["name_en"]
            # name_native is optional (iso639 does not always carry it).
            assert "name_native" in item

    async def test_includes_ukrainian_and_cantonese(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/config/languages")
        codes = {item["code"]: item for item in resp.json()["items"]}
        assert codes["ukr"]["name_en"] == "Ukrainian"
        # Cantonese (yue) — only reachable via 639-3; sanity-check it is in.
        assert "yue" in codes
