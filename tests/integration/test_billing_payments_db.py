"""The payment port on a live database, over HTTP (BIL-PAY).

The fake provider plays the payment provider: a checkout opened through the
portal or with the tenant's key, its purchase's webhook, refunds decided,
a chargeback, an event of no interest. Every request commits like the
deployed API (``get_session`` gives real sessions); the rows a test wrote are
removed afterwards. Also here: the migration down and up, and the locks of
the events table.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import (
    Connection,
    Table,
    create_engine,
    delete,
    func,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
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
from course_supporter.billing.payments.catalog import Catalog, Package
from course_supporter.billing.payments.fake import FAKE, SIGNATURE_HEADER, FakeProvider
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.billing.refunds import refund_hold
from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingBatch,
    BillingConsent,
    BillingMovement,
    BillingProviderEvent,
    BillingRefund,
    Student,
    Tenant,
)

pytestmark = pytest.mark.requires_db

WEBHOOK = f"/api/v1/billing/webhooks/{FAKE}"
TERMS_CONSENT = "/api/v1/portal/billing/consent"
PACKAGES = "/api/v1/portal/billing/packages"
CHECKOUT = "/api/v1/portal/billing/checkout"
AUTHOR_CHECKOUT = "/api/v1/billing/checkout"
SHA = "c" * 64

STARTER = Package(
    id="starter", units=1000, prices_minor={"EUR": 990, "USD": 1090}, active=True
)
CATALOG = Catalog(
    packages=(
        STARTER,
        Package(id="retired", units=50, prices_minor={"EUR": 100}, active=False),
    ),
    providers={FAKE: {"starter": "fake_price_starter"}},
)


@dataclass
class World:
    tenant_id: uuid.UUID
    student: StudentContext
    tenant: TenantContext
    token: str
    fake: FakeProvider = field(default_factory=FakeProvider)
    providers: list[FakeProvider] = field(default_factory=list)
    """The providers switched on (the fake by default)."""

    def event(self, name: str) -> str:
        """An event id this test owns (removed afterwards)."""
        return f"evt_{self.token}_{name}"


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[World]:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-pay-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        student = Student(tenant_id=tenant.id, external_id="pay-student")
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
        token=uuid.uuid4().hex[:12],
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
        batches = list(
            (
                await session.execute(
                    select(BillingBatch.id).where(BillingBatch.account_id.in_(accounts))
                )
            ).scalars()
        )
        await session.execute(
            delete(BillingProviderEvent).where(
                or_(
                    BillingProviderEvent.provider_event_id.like(f"evt_{made.token}_%"),
                    BillingProviderEvent.account_id.in_(accounts),
                )
            )
        )
        for model, column in (
            (BillingMovement, BillingMovement.account_id),
            (BillingRefund, BillingRefund.account_id),
            (BillingBatch, BillingBatch.account_id),
            (BillingConsent, BillingConsent.account_id),
        ):
            await session.execute(delete(model).where(column.in_(accounts)))
        await session.execute(
            delete(BillingAuditEntry).where(
                BillingAuditEntry.entity_id.in_(accounts + batches)
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


async def _consent(client: AsyncClient) -> None:
    response = await client.post(
        TERMS_CONSENT,
        json={
            "terms_version": get_settings().billing_terms_version,
            "text_sha256": SHA,
        },
    )
    assert response.status_code == 201, response.text


async def _send(client: AsyncClient, signed: tuple[dict[str, str], bytes]) -> Response:
    headers, body = signed
    return await client.post(WEBHOOK, content=body, headers=headers)


async def _checkout(client: AsyncClient, world: World, **body: Any) -> str:
    """A student's checkout of the starter package; its id."""
    response = await client.post(CHECKOUT, json={"package_id": "starter", **body})
    assert response.status_code == 200, response.text
    checkout_id = response.json()["checkout_id"]
    assert isinstance(checkout_id, str)
    return checkout_id


async def _account(
    session_factory: async_sessionmaker[AsyncSession], world: World, kind: str
) -> BillingAccount:
    async with session_factory() as session:
        return (
            await session.execute(
                select(BillingAccount).where(
                    BillingAccount.tenant_id == world.tenant_id,
                    BillingAccount.kind == kind,
                )
            )
        ).scalar_one()


async def _batches(
    session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> list[BillingBatch]:
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingBatch)
            .where(BillingBatch.account_id == account_id)
            .order_by(BillingBatch.created_at)
        )
        return list(rows.scalars())


async def _event(
    session_factory: async_sessionmaker[AsyncSession], event_id: str
) -> BillingProviderEvent | None:
    async with session_factory() as session:
        return (
            await session.execute(
                select(BillingProviderEvent).where(
                    BillingProviderEvent.provider == FAKE,
                    BillingProviderEvent.provider_event_id == event_id,
                )
            )
        ).scalar_one_or_none()


async def _count(session_factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# ── Checkout → purchase → units ────────────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestPurchase:
    async def test_checkout_then_webhook_credits_the_package(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            await _consent(client)
            checkout_id = await _checkout(client, world)
            signed = world.fake.purchase_webhook(
                checkout_id, event_id=world.event("paid")
            )
            response = await _send(client, signed)

        assert response.status_code == 200
        assert response.content == b""
        account = await _account(session_factory, world, "student")
        request = world.fake.checkouts[checkout_id]
        assert request.account_id == account.id  # the custom data, sent ...
        [batch] = await _batches(session_factory, account.id)  # ... and returned
        assert (batch.source, batch.units) == ("purchase", STARTER.units)
        assert (batch.amount_paid_minor, batch.currency) == (990, "EUR")
        token = checkout_id.removeprefix("fake_chk_")
        assert (
            batch.provider,
            batch.provider_transaction_id,
            batch.provider_line_id,
        ) == (
            FAKE,
            f"fake_txn_{token}",
            f"fake_line_{token}",
        )
        event = await _event(session_factory, world.event("paid"))
        assert event is not None
        assert (event.event_type, event.state, event.error) == (
            "purchase_completed",
            "processed",
            None,
        )
        assert (event.account_id, event.package_id, event.amount_minor) == (
            account.id,
            "starter",
            990,
        )
        assert event.provider_transaction_id == batch.provider_transaction_id
        assert event.body_sha256 == hashlib.sha256(signed[1]).hexdigest()
        assert event.provider_event_type == "purchase.completed"

    async def test_the_amount_and_currency_are_the_providers(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """What was paid comes from the provider (tax included); the units
        from the catalog."""
        async with _client() as client:
            await _consent(client)
            checkout_id = await _checkout(client, world)
            signed = world.fake.purchase_webhook(
                checkout_id,
                currency="USD",
                amount_minor=1308,
                event_id=world.event("usd"),
            )
            assert (await _send(client, signed)).status_code == 200
        account = await _account(session_factory, world, "student")
        [batch] = await _batches(session_factory, account.id)
        assert (batch.units, batch.amount_paid_minor, batch.currency) == (
            1000,
            1308,
            "USD",
        )

    async def test_a_repeated_event_changes_nothing(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            await _consent(client)
            checkout_id = await _checkout(client, world)
            signed = world.fake.purchase_webhook(
                checkout_id, event_id=world.event("twice")
            )
            first = await _send(client, signed)
            event = await _event(session_factory, world.event("twice"))
            movements = await _count(session_factory, BillingMovement)
            again = await _send(client, signed)

        assert (first.status_code, again.status_code) == (200, 200)
        assert again.content == first.content
        account = await _account(session_factory, world, "student")
        assert len(await _batches(session_factory, account.id)) == 1
        assert await _count(session_factory, BillingMovement) == movements
        repeat = await _event(session_factory, world.event("twice"))
        assert event is not None and repeat is not None
        assert (repeat.id, repeat.processed_at) == (event.id, event.processed_at)

    async def test_the_same_purchase_under_a_new_event_id_is_not_credited_twice(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """The journal's own key — the provider's line — holds too."""
        async with _client() as client:
            await _consent(client)
            checkout_id = await _checkout(client, world)
            for name in ("one", "two"):
                signed = world.fake.purchase_webhook(
                    checkout_id, event_id=world.event(name)
                )
                assert (await _send(client, signed)).status_code == 200
        account = await _account(session_factory, world, "student")
        assert len(await _batches(session_factory, account.id)) == 1
        for name in ("one", "two"):
            event = await _event(session_factory, world.event(name))
            assert event is not None and event.state == "processed"


@pytest.mark.usefixtures("signed_in")
class TestRefusedAndFailed:
    @pytest.mark.parametrize(
        "headers",
        [{}, {SIGNATURE_HEADER: "0" * 64}],
        ids=["no signature", "a wrong signature"],
    )
    async def test_without_a_valid_signature_nothing_is_recorded(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        headers: dict[str, str],
    ) -> None:
        async with _client() as client:
            await _consent(client)
            checkout_id = await _checkout(client, world)
            _, body = world.fake.purchase_webhook(
                checkout_id, event_id=world.event("forged")
            )
            response = await client.post(WEBHOOK, content=body, headers=headers)
        assert response.status_code == 400
        assert response.json() == {"detail": "Bad Request"}
        assert await _event(session_factory, world.event("forged")) is None
        account = await _account(session_factory, world, "student")
        assert await _batches(session_factory, account.id) == []

    async def test_a_purchase_for_no_account_fails_and_is_answered_200(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        nobody = uuid.uuid4()
        signed = world.fake.webhook(
            "purchase.completed",
            {
                "custom_data": {"account_id": str(nobody), "package_id": "starter"},
                "transaction_id": f"txn_{world.token}",
                "line_id": f"line_{world.token}",
                "amount_minor": 990,
                "currency": "EUR",
            },
            event_id=world.event("nobody"),
        )
        async with _client() as client:
            response = await _send(client, signed)
        assert response.status_code == 200  # the provider need not repeat it
        event = await _event(session_factory, world.event("nobody"))
        assert event is not None
        assert (event.state, event.account_id) == ("failed", nobody)
        assert event.error is not None and f"account {nobody} not found" in event.error
        async with session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count()).where(
                        BillingBatch.provider_line_id == f"line_{world.token}"
                    )
                )
                == 0
            )

    async def test_a_purchase_for_a_closed_account_fails_and_is_answered_200(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            await _consent(client)
            checkout_id = await _checkout(client, world)
            account = await _account(session_factory, world, "student")
            async with session_factory() as session:
                await session.execute(
                    update(BillingAccount)
                    .where(BillingAccount.id == account.id)
                    .values(status="closed", closed_at=datetime.now(UTC))
                )
                await session.commit()
            signed = world.fake.purchase_webhook(
                checkout_id, event_id=world.event("closed")
            )
            response = await _send(client, signed)
        assert response.status_code == 200
        event = await _event(session_factory, world.event("closed"))
        assert event is not None and event.state == "failed"
        assert event.error is not None and "is closed" in event.error
        assert await _batches(session_factory, account.id) == []

    async def test_a_failure_mid_way_rolls_back_and_the_repeat_succeeds(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            await _consent(client)
            checkout_id = await _checkout(client, world)
            signed = world.fake.purchase_webhook(
                checkout_id, event_id=world.event("retry")
            )
            # The batch and its credit are written, then the journal breaks.
            broken = AsyncMock(side_effect=RuntimeError("account 42 at 3 Main St"))
            with patch("course_supporter.billing.ledger.repay_differences", broken):
                failed = await _send(client, signed)
            account = await _account(session_factory, world, "student")
            assert broken.await_count == 1
            assert failed.status_code == 500
            assert failed.json() == {"detail": "Internal server error"}
            assert await _batches(session_factory, account.id) == []  # rolled back
            event = await _event(session_factory, world.event("retry"))
            assert event is not None
            # What failed, without the message: it might quote personal data.
            assert (event.state, event.error) == ("failed", "RuntimeError")

            again = await _send(client, signed)

        assert again.status_code == 200
        assert len(await _batches(session_factory, account.id)) == 1
        retried = await _event(session_factory, world.event("retry"))
        assert retried is not None
        assert (retried.id, retried.state, retried.error) == (
            event.id,
            "processed",
            None,
        )
        assert retried.received_at == event.received_at
        assert retried.processed_at > event.processed_at

    async def test_an_unknown_package_fails_with_500(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            await _consent(client)
            await _checkout(client, world)
            account = await _account(session_factory, world, "student")
            signed = world.fake.webhook(
                "purchase.completed",
                {
                    "custom_data": {"account_id": str(account.id), "package_id": "x"},
                    "transaction_id": f"txn_{world.token}",
                    "line_id": f"line_{world.token}",
                    "amount_minor": 990,
                    "currency": "EUR",
                },
                event_id=world.event("unknown-package"),
            )
            response = await _send(client, signed)
        assert response.status_code == 500
        event = await _event(session_factory, world.event("unknown-package"))
        assert event is not None and event.state == "failed"
        assert event.error == "BillingNotFoundError: package 'x' is not in the catalog"


# ── Refunds, chargeback, other events ──────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestOtherEvents:
    async def _paid_batch(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> BillingBatch:
        async with _client() as client:
            await _consent(client)
            checkout_id = await _checkout(client, world)
            signed = world.fake.purchase_webhook(
                checkout_id, event_id=world.event("paid")
            )
            assert (await _send(client, signed)).status_code == 200
        account = await _account(session_factory, world, "student")
        [batch] = await _batches(session_factory, account.id)
        return batch

    async def _hold(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        batch: BillingBatch,
        units: int,
        adjustment: str,
    ) -> None:
        async with session_factory() as session:
            await refund_hold(
                session,
                account_id=batch.account_id,
                batch_id=batch.id,
                units=units,
                provider=FAKE,
                provider_adjustment_id=adjustment,
            )
            await session.commit()

    async def _refund(
        self, session_factory: async_sessionmaker[AsyncSession], adjustment: str
    ) -> BillingRefund:
        async with session_factory() as session:
            return (
                await session.execute(
                    select(BillingRefund).where(
                        BillingRefund.provider == FAKE,
                        BillingRefund.provider_adjustment_id == adjustment,
                    )
                )
            ).scalar_one()

    async def _kinds(
        self, session_factory: async_sessionmaker[AsyncSession], account: uuid.UUID
    ) -> list[str]:
        async with session_factory() as session:
            rows = await session.execute(
                select(BillingMovement.kind)
                .where(BillingMovement.account_id == account)
                .order_by(BillingMovement.created_at, BillingMovement.id)
            )
            return list(rows.scalars())

    async def test_refunds_are_decided_through_the_journal(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        batch = await self._paid_batch(session_factory, world)
        approved, rejected = f"adj_{world.token}_a", f"adj_{world.token}_r"
        await self._hold(session_factory, batch, 100, approved)
        await self._hold(session_factory, batch, 200, rejected)
        async with _client() as client:
            yes = world.fake.refund_webhook(
                approved, approved=True, event_id=world.event("approved")
            )
            no = world.fake.refund_webhook(
                rejected, approved=False, event_id=world.event("rejected")
            )
            assert (await _send(client, yes)).status_code == 200
            assert (await _send(client, no)).status_code == 200

        assert (await self._refund(session_factory, approved)).state == "approved"
        assert (await self._refund(session_factory, rejected)).state == "rejected"
        assert await self._kinds(session_factory, batch.account_id) == [
            "credit",
            "refund_hold",
            "refund_hold",
            "refund_completed",
            "refund_rejected",
        ]
        for name, adjustment in (("approved", approved), ("rejected", rejected)):
            event = await _event(session_factory, world.event(name))
            assert event is not None and event.state == "processed"
            assert event.provider_adjustment_id == adjustment

    async def test_a_decision_on_no_refund_fails_with_500_for_a_repeat(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """The provider may decide before the hold is committed: the repeat
        finds it."""
        batch = await self._paid_batch(session_factory, world)
        adjustment = f"adj_{world.token}_early"
        signed = world.fake.refund_webhook(
            adjustment, approved=True, event_id=world.event("early")
        )
        async with _client() as client:
            assert (await _send(client, signed)).status_code == 500
            event = await _event(session_factory, world.event("early"))
            assert event is not None and event.state == "failed"
            assert event.error == (
                f"BillingNotFoundError: no refund for fake adjustment {adjustment}"
            )
            await self._hold(session_factory, batch, 10, adjustment)
            assert (await _send(client, signed)).status_code == 200
        assert (await self._refund(session_factory, adjustment)).state == "approved"

    async def test_a_chargeback_is_recorded_and_the_journal_untouched(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        batch = await self._paid_batch(session_factory, world)
        before = await self._kinds(session_factory, batch.account_id)
        signed = world.fake.webhook(
            "chargeback.created",
            {
                "custom_data": {"account_id": str(batch.account_id)},
                "transaction_id": batch.provider_transaction_id,
                "amount_minor": 990,
                "currency": "EUR",
            },
            event_id=world.event("chargeback"),
        )
        async with _client() as client:
            assert (await _send(client, signed)).status_code == 200
        assert await self._kinds(session_factory, batch.account_id) == before
        assert len(await _batches(session_factory, batch.account_id)) == 1
        event = await _event(session_factory, world.event("chargeback"))
        assert event is not None
        assert (event.event_type, event.state, event.account_id) == (
            "chargeback",
            "recorded",
            batch.account_id,
        )

    async def test_an_unknown_event_is_ignored(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        signed = world.fake.webhook(
            "customer.updated", {"email": "x@example.com"}, event_id=world.event("u")
        )
        async with _client() as client:
            assert (await _send(client, signed)).status_code == 200
        event = await _event(session_factory, world.event("u"))
        assert event is not None
        assert (event.event_type, event.state, event.provider_event_type) == (
            "unknown",
            "ignored",
            "customer.updated",
        )


# ── The address of a webhook ───────────────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestWebhookAddress:
    @pytest.mark.parametrize("name", ["paddle", "invoice", "FAKE"])
    async def test_a_provider_not_switched_on_is_404(self, name: str) -> None:
        async with _client() as client:
            response = await client.post(
                f"/api/v1/billing/webhooks/{name}", content=b"{}"
            )
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}

    async def test_with_no_provider_switched_on_the_fake_is_404(
        self, world: World
    ) -> None:
        world.providers.clear()
        async with _client() as client:
            response = await _send(client, world.fake.webhook("x", {}))
        assert response.status_code == 404

    async def test_a_body_too_large_is_413(self, world: World) -> None:
        body = b"x" * (256 * 1024 + 1)
        async with _client() as client:
            response = await client.post(
                WEBHOOK, content=body, headers={SIGNATURE_HEADER: world.fake.sign(body)}
            )
        assert response.status_code == 413


# ── Portal: packages and checkout ──────────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestPortalCheckout:
    async def test_packages_on_sale_without_providers_ids(self) -> None:
        async with _client() as client:
            response = await client.get(PACKAGES)
        assert response.status_code == 200
        assert response.json() == {
            "packages": [
                {
                    "id": "starter",
                    "units": 1000,
                    "prices_minor": {"EUR": 990, "USD": 1090},
                }
            ]
        }
        assert "fake_price_starter" not in response.text

    async def test_without_consent_it_is_409(self, world: World) -> None:
        async with _client() as client:
            response = await client.post(CHECKOUT, json={"package_id": "starter"})
        assert response.status_code == 409
        assert get_settings().billing_terms_version in response.json()["detail"]
        assert world.fake.checkouts == {}

    async def test_without_providers_it_is_503(self, world: World) -> None:
        world.providers.clear()
        async with _client() as client:
            refused = await client.post(CHECKOUT, json={"package_id": "starter"})
            await _consent(client)
            still = await client.post(CHECKOUT, json={"package_id": "starter"})
        assert (refused.status_code, still.status_code) == (503, 503)
        assert still.json() == {"detail": "Payment is unavailable."}

    async def test_with_the_fake_it_is_the_link(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            await _consent(client)
            response = await client.post(
                CHECKOUT, json={"package_id": "starter", "provider": FAKE}
            )
        assert response.status_code == 200
        answer = response.json()
        assert answer["provider"] == FAKE
        assert answer["checkout_url"].endswith(answer["checkout_id"])
        request = world.fake.checkouts[answer["checkout_id"]]
        account = await _account(session_factory, world, "student")
        assert request.account_id == account.id
        assert request.provider_item == "fake_price_starter"
        assert request.language == "ukr"
        portal = get_settings().portal_base_url.rstrip("/")
        assert request.success_url == (
            f"{portal}/{world.tenant_id}/billing?checkout=success"
        )

    @pytest.mark.parametrize(
        "body",
        [
            {"package_id": "nope"},
            {"package_id": "retired"},
            {"package_id": "starter", "provider": "paddle"},
        ],
        ids=["unknown package", "package off sale", "provider not switched on"],
    )
    async def test_nothing_on_sale_is_404(self, body: dict[str, str]) -> None:
        async with _client() as client:
            await _consent(client)
            response = await client.post(CHECKOUT, json=body)
        assert response.status_code == 404

    async def test_a_body_with_more_is_422(self) -> None:
        async with _client() as client:
            await _consent(client)
            response = await client.post(
                CHECKOUT, json={"package_id": "starter", "units": 10**6}
            )
        assert response.status_code == 422


async def test_the_portal_routes_need_a_portal_session() -> None:
    async with _client() as client:
        assert (await client.get(PACKAGES)).status_code == 401
        response = await client.post(CHECKOUT, json={"package_id": "starter"})
        assert response.status_code == 401


# ── The author's checkout ──────────────────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestAuthorCheckout:
    async def test_the_units_go_to_the_tenants_author_account(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            response = await client.post(
                AUTHOR_CHECKOUT, json={"package_id": "starter"}
            )
            assert response.status_code == 200, response.text
            checkout_id = response.json()["checkout_id"]
            signed = world.fake.purchase_webhook(
                checkout_id, event_id=world.event("author")
            )
            assert (await _send(client, signed)).status_code == 200
        author = await _account(session_factory, world, "author")
        request = world.fake.checkouts[checkout_id]
        assert request.account_id == author.id
        assert request.language is None
        base = get_settings().author_app_base_url.rstrip("/")
        assert request.success_url == f"{base}/billing?checkout=success"
        [batch] = await _batches(session_factory, author.id)
        assert (batch.units, batch.provider) == (1000, FAKE)

    async def test_it_needs_the_prep_scope(self, world: World) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
            tenant_id=world.tenant_id,
            tenant_name="t",
            scopes=["check"],
            plan_id="basic",
            key_prefix="cs_test",
        )
        async with _client() as client:
            response = await client.post(
                AUTHOR_CHECKOUT, json={"package_id": "starter"}
            )
        assert response.status_code == 403

    async def test_without_providers_it_is_503(self, world: World) -> None:
        world.providers.clear()
        async with _client() as client:
            response = await client.post(
                AUTHOR_CHECKOUT, json={"package_id": "starter"}
            )
        assert response.status_code == 503


# ── Schema: the events table, the migration ────────────────────────────


class TestEventsTable:
    async def test_it_has_no_foreign_key_and_no_insert_only_trigger(
        self, db_session: AsyncSession
    ) -> None:
        keys = await db_session.scalar(
            text(
                "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND "
                "conrelid = 'billing_provider_events'::regclass"
            )
        )
        triggers = await db_session.scalar(
            text(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND "
                "tgrelid = 'billing_provider_events'::regclass"
            )
        )
        assert (keys, triggers) == (0, 0)

    async def test_one_row_per_event_of_a_provider(
        self, db_session: AsyncSession
    ) -> None:
        def row(provider: str) -> BillingProviderEvent:
            return BillingProviderEvent(
                provider=provider,
                provider_event_id="evt_unique",
                event_type="unknown",
                provider_event_type="x",
                state="ignored",
                body_sha256=SHA,
                processed_at=datetime.now(UTC),
            )

        db_session.add_all([row("one"), row("two")])
        await db_session.flush()  # the same id at another provider is another event
        with pytest.raises(IntegrityError, match="uq_billing_provider_events_event"):
            async with db_session.begin_nested():
                db_session.add(row("one"))
                await db_session.flush()

    async def test_comments_are_the_orms(self, db_session: AsyncSession) -> None:
        orm: Table = BillingProviderEvent.__table__  # type: ignore[assignment]
        rows = (
            await db_session.execute(
                text(
                    "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM "
                    "pg_attribute a WHERE a.attrelid = 'billing_provider_events'"
                    "::regclass AND a.attnum > 0 AND NOT a.attisdropped"
                )
            )
        ).all()
        assert {name: comment for name, comment in rows} == {
            c.name: c.comment for c in orm.c
        }
        comment = await db_session.scalar(
            text("SELECT obj_description('billing_provider_events'::regclass)")
        )
        assert comment == orm.comment


def _paddle_purchase_before_the_revision(
    conn: Connection, account_id: uuid.UUID, batch_id: uuid.UUID
) -> None:
    conn.execute(
        text(
            "INSERT INTO billing_accounts (id, kind, tenant_id) "
            "VALUES (:a, 'author', :t)"
        ),
        {"a": account_id, "t": uuid.uuid4()},
    )
    conn.execute(
        text(
            "INSERT INTO billing_batches (id, account_id, source, units, expires_at, "
            "amount_paid_minor, currency, paddle_transaction_id, paddle_line_id) "
            "VALUES (:b, :a, 'purchase', 100, now() + interval '6 months', 1000, "
            "'EUR', 'txn_old', :line)"
        ),
        {"b": batch_id, "a": account_id, "line": f"line_{batch_id.hex}"},
    )


def _forget(account_id: uuid.UUID) -> None:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM billing_batches WHERE account_id = :a"), {"a": account_id}
        )
        conn.execute(
            text("DELETE FROM billing_accounts WHERE id = :a"), {"a": account_id}
        )
    engine.dispose()


def test_the_migration_goes_down_and_up() -> None:
    """Down: the Paddle names and the old key are back, the events table is
    gone; a Paddle purchase written there comes up as provider 'paddle'."""
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    account_id, batch_id = uuid.uuid4(), uuid.uuid4()
    try:
        command.downgrade(cfg, "bilb_unit_ledger")
        with engine.begin() as conn:
            names = set(inspect(conn).get_table_names())
            assert "billing_provider_events" not in names
            batches = {c["name"] for c in inspect(conn).get_columns("billing_batches")}
            assert {"paddle_transaction_id", "paddle_line_id"} <= batches
            assert "provider" not in batches
            refunds = {c["name"] for c in inspect(conn).get_columns("billing_refunds")}
            assert "paddle_adjustment_id" in refunds and "provider" not in refunds
            _paddle_purchase_before_the_revision(conn, account_id, batch_id)
    finally:
        command.upgrade(cfg, "head")
    try:
        with engine.connect() as conn:
            assert "billing_provider_events" in inspect(conn).get_table_names()
            row = conn.execute(
                text(
                    "SELECT provider, provider_transaction_id FROM billing_batches "
                    "WHERE id = :b"
                ),
                {"b": batch_id},
            ).one()
            assert tuple(row) == ("paddle", "txn_old")
            trigger = conn.scalar(
                text(
                    "SELECT tgenabled FROM pg_trigger "
                    "WHERE tgname = 'billing_batches_insert_only'"
                )
            )
            assert trigger == "O"  # enabled again after the backfill
    finally:
        _forget(account_id)
        engine.dispose()


def test_the_downgrade_is_refused_while_another_provider_has_a_purchase() -> None:
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    account_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO billing_accounts (id, kind, tenant_id) "
                "VALUES (:a, 'author', :t)"
            ),
            {"a": account_id, "t": uuid.uuid4()},
        )
        conn.execute(
            text(
                "INSERT INTO billing_batches (id, account_id, source, units, "
                "expires_at, amount_paid_minor, currency, provider, "
                "provider_transaction_id, provider_line_id) VALUES (:b, :a, "
                "'purchase', 10, now() + interval '6 months', 500, 'EUR', "
                "'invoice', :n, :n)"
            ),
            {"b": uuid.uuid4(), "a": account_id, "n": f"INV-{account_id.hex[:12]}"},
        )
    try:
        with pytest.raises(RuntimeError, match="other than Paddle"):
            command.downgrade(cfg, "bilb_unit_ledger")
        with engine.connect() as conn:
            current = conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert current == "bilpay_payment_port"
    finally:
        _forget(account_id)
        engine.dispose()
