"""The portal's billing-terms routes over HTTP, on a live database (BIL-A).

``GET /portal/billing/terms`` and ``POST /portal/billing/consent`` run under
the student's own portal session; the database is real (committed rows,
removed afterwards). Also locked here: billing has no route but the
student's, the author's checkout and the providers' webhook (BIL-PAY) — none
for an administrator: by operator decision of 2026-11-05 the administrator
works through ``scripts/billing_admin.py`` only, until there is a decision on
how an administrator signs in.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_student
from course_supporter.auth.context import StudentContext
from course_supporter.config import get_settings
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    BillingAccount,
    BillingConsent,
    Student,
    Tenant,
)

pytestmark = pytest.mark.requires_db

TERMS = "/api/v1/portal/billing/terms"
CONSENT = "/api/v1/portal/billing/consent"
SHA = "b" * 64


@pytest.fixture()
async def student(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[StudentContext]:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-a-portal-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        row = Student(tenant_id=tenant.id, external_id="portal-student")
        session.add(row)
        await session.commit()
        context = StudentContext(
            student_id=row.id, tenant_id=tenant.id, login="s", display_name=None
        )

    yield context

    async with session_factory() as session:
        accounts = select(BillingAccount.id).where(
            BillingAccount.tenant_id == context.tenant_id
        )
        await session.execute(
            delete(BillingConsent).where(BillingConsent.account_id.in_(accounts))
        )
        await session.execute(
            delete(BillingAccount).where(BillingAccount.tenant_id == context.tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == context.tenant_id))
        await session.commit()


@pytest.fixture()
def signed_in(
    session_factory: async_sessionmaker[AsyncSession], student: StudentContext
) -> Iterator[None]:
    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_student] = lambda: student
    yield
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.usefixtures("signed_in")
class TestConsentRoutes:
    async def test_required_then_recorded_then_not_required(self) -> None:
        current = get_settings().billing_terms_version
        async with _client() as client:
            before = await client.get(TERMS)
            given = await client.post(
                CONSENT, json={"terms_version": current, "text_sha256": SHA}
            )
            again = await client.post(
                CONSENT, json={"terms_version": current, "text_sha256": SHA}
            )
            after = await client.get(TERMS)

        assert before.status_code == 200
        assert before.json() == {
            "terms_version": current,
            "consent_required": True,
            "consented_at": None,
        }
        assert given.status_code == 201
        assert given.json()["terms_version"] == current
        assert again.status_code == 200  # agreed before: the earlier record
        assert again.json() == given.json()
        assert after.json()["consent_required"] is False
        assert after.json()["consented_at"] == given.json()["consented_at"]

    async def test_another_revision_is_a_conflict(self) -> None:
        async with _client() as client:
            response = await client.post(
                CONSENT, json={"terms_version": "2000-01-01", "text_sha256": SHA}
            )
            status = await client.get(TERMS)
        assert response.status_code == 409
        assert get_settings().billing_terms_version in response.json()["detail"]
        assert status.json()["consent_required"] is True

    async def test_a_fingerprint_is_64_lowercase_hex(self) -> None:
        current = get_settings().billing_terms_version
        async with _client() as client:
            response = await client.post(
                CONSENT, json={"terms_version": current, "text_sha256": "XYZ"}
            )
        assert response.status_code == 422


async def test_terms_need_a_portal_session() -> None:
    async with _client() as client:
        assert (await client.get(TERMS)).status_code == 401
        response = await client.post(
            CONSENT, json={"terms_version": "x", "text_sha256": SHA}
        )
        assert response.status_code == 401


def test_billing_has_no_administrator_route() -> None:
    billing = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute) and "billing" in route.path
        for method in route.methods
    }
    assert billing == {
        (TERMS, "GET"),
        (CONSENT, "POST"),
        ("/api/v1/portal/billing/packages", "GET"),
        ("/api/v1/portal/billing/checkout", "POST"),
        ("/api/v1/billing/checkout", "POST"),  # the author's, the tenant's key
        ("/api/v1/billing/webhooks/{provider}", "POST"),  # the provider's signature
    }
