"""The routes of the purchase screens on a live database, over HTTP (BIL-PAY).

The fake provider (always in test mode) and Creem (its API replaced by an
``httpx.MockTransport``: nothing leaves the process) play the payment
provider. Every request commits like the deployed API; the rows a test wrote
are removed afterwards.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_billing_catalog,
    get_current_student,
    get_current_tenant,
    get_payment_registry,
    get_session,
)
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.billing import admin
from course_supporter.billing.payments.catalog import Catalog, Package
from course_supporter.billing.payments.creem import (
    CREEM,
    LIVE_API_BASE,
    CreemProvider,
)
from course_supporter.billing.payments.fake import FAKE, FakeProvider
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingBatch,
    BillingCheckout,
    BillingConsent,
    BillingDifference,
    BillingMovement,
    BillingProviderEvent,
    BillingRefund,
    BillingReservation,
    Student,
    Tenant,
)

pytestmark = pytest.mark.requires_db

STARTER = Package(id="starter", units=1000, prices_minor={"EUR": 990}, active=True)
CATALOG = Catalog(
    packages=(
        STARTER,
        Package(id="large", units=5000, prices_minor={"EUR": 3990}, active=True),
        Package(id="retired", units=50, prices_minor={"EUR": 100}, active=False),
    ),
    providers={
        FAKE: {"starter": "fake_price_starter", "large": "fake_price_large"},
        CREEM: {"starter": "prod_starter", "large": "prod_large"},
    },
)
SHA = "e" * 64
WEBHOOK = f"/api/v1/billing/webhooks/{FAKE}"


class Who:
    """The two payers, by the address of their billing routes."""

    AUTHOR = "/api/v1/billing"
    STUDENT = "/api/v1/portal/billing"


PAYERS = [Who.AUTHOR, Who.STUDENT]
PAYER_IDS = ["author", "student"]


@dataclass
class World:
    tenant_id: uuid.UUID
    student: StudentContext
    tenant: TenantContext
    fake: FakeProvider = field(default_factory=FakeProvider)
    providers: list[Any] = field(default_factory=list)
    """The providers switched on (the fake by default)."""
    sent: list[httpx.Request] = field(default_factory=list)
    """What a Creem adapter sent to Creem's API."""

    def creem(self, api_base: str = LIVE_API_BASE) -> CreemProvider:
        """Creem behind a mock of its API."""

        def answer(request: httpx.Request) -> httpx.Response:
            self.sent.append(request)
            return httpx.Response(
                200,
                json={
                    "id": f"ch_{len(self.sent)}",
                    "checkout_url": f"https://checkout.creem.io/ch_{len(self.sent)}",
                },
            )

        return CreemProvider(
            api_key="creem_key_for_tests_only",
            webhook_secret="whsec_for_tests_only",
            api_base=api_base,
            transport=httpx.MockTransport(answer),
        )


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[World]:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-screens-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        student = Student(tenant_id=tenant.id, external_id="screens-student")
        session.add(student)
        await session.commit()
    made = World(
        tenant_id=tenant.id,
        student=StudentContext(
            student_id=student.id,
            tenant_id=tenant.id,
            login="s",
            display_name=None,
            preferred_language="ukr",
        ),
        tenant=TenantContext(
            tenant_id=tenant.id,
            tenant_name=tenant.name,
            scopes=["prep"],
            plan_id="basic",
            key_prefix="cs_test",
        ),
    )
    made.providers.append(made.fake)

    yield made

    async with session_factory() as session:
        accounts = list(
            (
                await session.execute(
                    select(BillingAccount.id).where(
                        BillingAccount.tenant_id == made.tenant_id
                    )
                )
            ).scalars()
        )
        owned = [
            *(
                await session.execute(
                    select(BillingBatch.id).where(BillingBatch.account_id.in_(accounts))
                )
            ).scalars(),
            *(
                await session.execute(
                    select(BillingRefund.id).where(
                        BillingRefund.account_id.in_(accounts)
                    )
                )
            ).scalars(),
        ]
        await session.execute(
            delete(BillingProviderEvent).where(
                BillingProviderEvent.account_id.in_(accounts)
            )
        )
        await session.execute(
            delete(BillingMovement).where(
                or_(
                    BillingMovement.account_id.in_(accounts),
                    BillingMovement.beneficiary_account_id.in_(accounts),
                )
            )
        )
        for model, column in (
            (BillingRefund, BillingRefund.account_id),
            (BillingReservation, BillingReservation.account_id),
            (BillingDifference, BillingDifference.account_id),
            (BillingCheckout, BillingCheckout.account_id),
            (BillingBatch, BillingBatch.account_id),
            (BillingConsent, BillingConsent.account_id),
        ):
            await session.execute(delete(model).where(column.in_(accounts)))
        await session.execute(
            delete(BillingAuditEntry).where(
                BillingAuditEntry.entity_id.in_(accounts + owned)
            )
        )
        await session.execute(
            delete(BillingAccount).where(BillingAccount.id.in_(accounts))
        )
        await session.execute(
            delete(Student).where(Student.tenant_id == made.tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == made.tenant_id))
        await session.commit()


@pytest.fixture()
def signed_in(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> Iterator[None]:
    async def _session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_student] = lambda: world.student
    app.dependency_overrides[get_current_tenant] = lambda: world.tenant
    app.dependency_overrides[get_payment_registry] = lambda: ProviderRegistry(
        world.providers
    )
    app.dependency_overrides[get_billing_catalog] = lambda: CATALOG
    yield
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _consent(client: AsyncClient, who: str) -> None:
    response = await client.post(
        f"{who}/consent",
        json={
            "terms_version": get_settings().billing_terms_version,
            "text_sha256": SHA,
        },
    )
    assert response.status_code == 201, response.text


async def _mark(
    session_factory: async_sessionmaker[AsyncSession],
    world: World,
    who: str,
    *,
    allowed: bool = True,
) -> uuid.UUID:
    """The administrator marks (unmarks) the payer's account; its id."""
    async with session_factory() as session:
        done = await admin.set_test_purchase(
            session,
            tenant_id=world.tenant_id,
            student_id=world.student.student_id if who == Who.STUDENT else None,
            allowed=allowed,
            actor="tests",
        )
        await session.commit()
    return done.account.id


async def _buy(client: AsyncClient, who: str, package: str = "starter") -> Response:
    return await client.post(f"{who}/checkout", json={"package_id": package})


def _code(response: Response) -> str:
    detail = response.json()["detail"]
    assert set(detail) == {"code", "details"}, detail
    code = detail["code"]
    assert isinstance(code, str)
    return code


async def _batches(
    session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> list[BillingBatch]:
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingBatch).where(BillingBatch.account_id == account_id)
        )
        return list(rows.scalars())


# ── The test-mode guard (operator 2026-11-15) ──────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestGuard:
    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_test_mode_and_an_unmarked_account_is_403(
        self, world: World, who: str
    ) -> None:
        async with _client() as client:
            await _consent(client, who)
            response = await _buy(client, who)
        assert response.status_code == 403
        assert response.json() == {
            "detail": {
                "code": "purchase_not_open",
                "details": "Purchase is not open yet.",
            }
        }
        assert world.fake.checkouts == {}  # the provider was not asked

    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_a_marked_account_opens_the_checkout(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        who: str,
    ) -> None:
        async with _client() as client:
            await _consent(client, who)
            account_id = await _mark(session_factory, world, who)
            response = await _buy(client, who)
        assert response.status_code == 200, response.text
        [request] = world.fake.checkouts.values()
        assert request.account_id == account_id

    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_in_live_mode_the_mark_is_not_read(
        self, world: World, who: str
    ) -> None:
        world.providers[:] = [world.creem(LIVE_API_BASE)]
        async with _client() as client:
            await _consent(client, who)
            response = await _buy(client, who)
        assert response.status_code == 200, response.text
        [sent] = world.sent
        assert sent.url.host == "api.creem.io"
        assert json.loads(sent.content)["product_id"] == "prod_starter"

    async def test_the_mark_of_the_author_does_not_open_the_students(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            await _consent(client, Who.STUDENT)
            await _mark(session_factory, world, Who.AUTHOR)
            response = await _buy(client, Who.STUDENT)
        assert (response.status_code, _code(response)) == (403, "purchase_not_open")

    async def test_a_purchase_paid_after_the_mark_was_cleared_is_credited(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """The webhook is not guarded: what was paid is credited."""
        async with _client() as client:
            await _consent(client, Who.STUDENT)
            account_id = await _mark(session_factory, world, Who.STUDENT)
            opened = await _buy(client, Who.STUDENT)
            await _mark(session_factory, world, Who.STUDENT, allowed=False)
            refused = await _buy(client, Who.STUDENT)
            headers, body = world.fake.purchase_webhook(opened.json()["checkout_id"])
            paid = await client.post(WEBHOOK, content=body, headers=headers)
        assert (refused.status_code, _code(refused)) == (403, "purchase_not_open")
        assert paid.status_code == 200
        [batch] = await _batches(session_factory, account_id)
        assert (batch.units, batch.provider) == (1000, FAKE)


# ── Packages ───────────────────────────────────────────────────────────


async def _accounts(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> list[BillingAccount]:
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingAccount).where(BillingAccount.tenant_id == world.tenant_id)
        )
        return list(rows.scalars())


@pytest.mark.usefixtures("signed_in")
class TestPackages:
    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_the_packages_on_sale_without_providers_ids(self, who: str) -> None:
        async with _client() as client:
            response = await client.get(f"{who}/packages")
        assert response.status_code == 200
        assert response.json()["packages"] == [
            {"id": "starter", "units": 1000, "prices_minor": {"EUR": 990}},
            {"id": "large", "units": 5000, "prices_minor": {"EUR": 3990}},
        ]
        assert "fake_price" not in response.text
        assert "prod_" not in response.text

    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_no_provider_is_payment_unavailable(
        self, world: World, who: str
    ) -> None:
        world.providers.clear()
        async with _client() as client:
            response = await client.get(f"{who}/packages")
        assert response.json()["purchase"] == {
            "open": False,
            "reason": "payment_unavailable",
        }

    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_test_mode_and_no_account_yet_is_not_open_and_creates_none(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        who: str,
    ) -> None:
        async with _client() as client:
            response = await client.get(f"{who}/packages")
        assert response.json()["purchase"] == {
            "open": False,
            "reason": "purchase_not_open",
        }
        assert await _accounts(session_factory, world) == []

    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_test_mode_and_an_unmarked_account_is_not_open(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        who: str,
    ) -> None:
        await _mark(session_factory, world, who, allowed=False)  # exists, unmarked
        async with _client() as client:
            response = await client.get(f"{who}/packages")
        assert response.json()["purchase"]["reason"] == "purchase_not_open"

    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_a_marked_account_is_open(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        who: str,
    ) -> None:
        await _mark(session_factory, world, who)
        async with _client() as client:
            response = await client.get(f"{who}/packages")
        assert response.json()["purchase"] == {"open": True, "reason": None}

    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_in_live_mode_it_is_open_to_anyone(
        self, world: World, who: str
    ) -> None:
        world.providers[:] = [world.creem(LIVE_API_BASE)]
        async with _client() as client:
            response = await client.get(f"{who}/packages")
        assert response.json()["purchase"] == {"open": True, "reason": None}

    async def test_the_authors_packages_need_the_prep_scope(self, world: World) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
            tenant_id=world.tenant_id,
            tenant_name="t",
            scopes=["check"],
            plan_id="basic",
            key_prefix="cs_test",
        )
        async with _client() as client:
            response = await client.get(f"{Who.AUTHOR}/packages")
        assert response.status_code == 403


async def test_the_packages_need_a_key_or_a_portal_session() -> None:
    async with _client() as client:
        portal = await client.get(f"{Who.STUDENT}/packages")
        author = await client.get(f"{Who.AUTHOR}/packages")
    assert (portal.status_code, author.status_code) == (401, 401)
