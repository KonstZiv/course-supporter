"""Creem end to end on a live database, over HTTP (BIL-PAY part 2).

The Creem adapter, its API replaced by an ``httpx.MockTransport`` (nothing
leaves the process): a checkout opened, ``checkout.completed`` → a batch of
provider ``creem``; refunds — one the journal held and one made in Creem's
dashboard; ``dispute.created`` → the free units held; the administrator's
``dispute-lost`` / ``dispute-won``. Also: the author's consent to the terms
(operator 2026-11-12) and the migration down and up.

The bodies follow Creem's samples (https://docs.creem.io/code/webhooks).
Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient, Response
from scripts import billing_admin
from sqlalchemy import create_engine, delete, inspect, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_billing_catalog,
    get_current_student,
    get_current_tenant,
    get_payment_registry,
    get_session,
)
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.billing import ledger
from course_supporter.billing.kinds import ActionKind, PayerSource
from course_supporter.billing.payments.catalog import Catalog, Package
from course_supporter.billing.payments.creem import (
    CREEM,
    SIGNATURE_HEADER,
    CreemProvider,
)
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.billing.refunds import refund_hold
from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingBatch,
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

WEBHOOK = f"/api/v1/billing/webhooks/{CREEM}"
STUDENT_CONSENT = "/api/v1/portal/billing/consent"
STUDENT_CHECKOUT = "/api/v1/portal/billing/checkout"
AUTHOR_TERMS = "/api/v1/billing/terms"
AUTHOR_CONSENT = "/api/v1/billing/consent"
AUTHOR_CHECKOUT = "/api/v1/billing/checkout"
SHA = "d" * 64
TEST_KEY = "creem_test_key_for_db_tests_only"
TEST_SECRET = "whsec_test_secret_for_db_tests_only"
PAID = 1198
"""What the payer paid for the starter package: 990 EUR cents + 21 % VAT."""

CATALOG = Catalog(
    packages=(
        Package(id="starter", units=1000, prices_minor={"EUR": 990}, active=True),
    ),
    providers={CREEM: {"starter": "prod_starter_test"}},
)


@dataclass
class World:
    tenant_id: uuid.UUID
    student: StudentContext
    tenant: TenantContext
    token: str
    sent: list[httpx.Request] = field(default_factory=list)
    """What the adapter sent to Creem's API."""
    creem: CreemProvider | None = None

    def id(self, prefix: str, name: str) -> str:
        """An id this test owns (events are removed afterwards)."""
        return f"{prefix}_{self.token}_{name}"

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        checkout_id = self.id("ch", str(len(self.sent)))
        return httpx.Response(
            200,
            json={
                "id": checkout_id,
                "object": "checkout",
                "status": "pending",
                "product": "prod_starter_test",
                "checkout_url": f"https://checkout.creem.io/{checkout_id}",
                "mode": "test",
            },
        )

    def metadata(self, index: int = -1) -> dict[str, Any]:
        """The metadata of a checkout the adapter opened (Creem echoes it)."""
        found = json.loads(self.sent[index].content)["metadata"]
        assert isinstance(found, dict)
        return found


def sign(payload: dict[str, Any]) -> tuple[dict[str, str], bytes]:
    body = json.dumps(payload).encode()
    signature = hmac.new(TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {SIGNATURE_HEADER: signature}, body


def checkout_completed(
    world: World, event: str, order: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": world.id("evt", event),
        "eventType": "checkout.completed",
        "created_at": 1763100000000,
        "object": {
            "id": world.id("ch", event),
            "object": "checkout",
            "order": {
                "object": "order",
                "id": order,
                "product": "prod_starter_test",
                "amount": 990,
                "currency": "EUR",
                "sub_total": 990,
                "tax_amount": PAID - 990,
                "amount_due": PAID,
                "amount_paid": PAID,
                "status": "paid",
                "type": "onetime",
                "transaction": world.id("tran", event),
                "mode": "test",
            },
            "product": {"id": "prod_starter_test", "object": "product"},
            "units": 1,
            "customer": {"id": "cust_1", "email": "customer@emaildomain"},
            "status": "completed",
            "metadata": metadata,
            "mode": "test",
        },
    }


def refund_created(
    world: World, event: str, refund: str, order: str, amount: int
) -> dict[str, Any]:
    return {
        "id": world.id("evt", event),
        "eventType": "refund.created",
        "created_at": 1763100100000,
        "object": {
            "id": refund,
            "object": "refund",
            "status": "succeeded",
            "refund_amount": amount,
            "refund_currency": "EUR",
            "reason": "requested_by_customer",
            "transaction": {"id": "tran_x", "object": "transaction", "order": order},
            "order": {"object": "order", "id": order, "currency": "EUR"},
            "mode": "test",
        },
    }


def dispute_created(
    world: World, event: str, dispute: str, order: str
) -> dict[str, Any]:
    return {
        "id": world.id("evt", event),
        "eventType": "dispute.created",
        "created_at": 1763100200000,
        "object": {
            "id": dispute,
            "object": "dispute",
            "amount": PAID,
            "currency": "EUR",
            "transaction": {"id": "tran_x", "object": "transaction", "order": order},
            "order": {"object": "order", "id": order, "currency": "EUR"},
            "checkout": {"id": "ch_x", "object": "checkout", "status": "completed"},
            "mode": "test",
        },
    }


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[World]:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-creem-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        student = Student(tenant_id=tenant.id, external_id="creem-student")
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
    made.creem = CreemProvider(
        api_key=TEST_KEY,
        webhook_secret=TEST_SECRET,
        transport=httpx.MockTransport(made.answer),
    )

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
        owned = list(
            (
                await session.execute(
                    select(BillingBatch.id).where(BillingBatch.account_id.in_(accounts))
                )
            ).scalars()
        ) + list(
            (
                await session.execute(
                    select(BillingRefund.id).where(
                        BillingRefund.account_id.in_(accounts)
                    )
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
            (BillingReservation, BillingReservation.account_id),
            (BillingDifference, BillingDifference.account_id),
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

    assert world.creem is not None
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_student] = lambda: world.student
    app.dependency_overrides[get_current_tenant] = lambda: world.tenant
    app.dependency_overrides[get_payment_registry] = lambda: ProviderRegistry(
        [world.creem]  # type: ignore[list-item]
    )
    app.dependency_overrides[get_billing_catalog] = lambda: CATALOG
    yield
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _send(client: AsyncClient, payload: dict[str, Any]) -> Response:
    headers, body = sign(payload)
    return await client.post(WEBHOOK, content=body, headers=headers)


async def _bought(
    session_factory: async_sessionmaker[AsyncSession], world: World, order: str
) -> BillingBatch:
    """A student's checkout of the starter package, paid; its batch."""
    async with _client() as client:
        consent = await client.post(
            STUDENT_CONSENT,
            json={
                "terms_version": get_settings().billing_terms_version,
                "text_sha256": SHA,
            },
        )
        assert consent.status_code in (200, 201), consent.text
        opened = await client.post(STUDENT_CHECKOUT, json={"package_id": "starter"})
        assert opened.status_code == 200, opened.text
        paid = checkout_completed(world, f"paid_{order}", order, world.metadata())
        assert (await _send(client, paid)).status_code == 200
    async with session_factory() as session:
        return (
            await session.execute(
                select(BillingBatch).where(
                    BillingBatch.provider == CREEM,
                    BillingBatch.provider_line_id == order,
                )
            )
        ).scalar_one()


async def _spend(
    session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    units: int,
) -> None:
    async with session_factory() as session:
        await ledger.settle(
            session,
            action_kind=ActionKind.REVIEW,
            action_key=str(uuid.uuid4()),
            fact_units=units,
            beneficiary_account_id=account_id,
            payer_source=PayerSource.DEFAULT,
            account_id=account_id,
        )
        await session.commit()


async def _journal(
    session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> list[tuple[str, int]]:
    async with session_factory() as session:
        return [(m.kind, m.units) for m in await ledger.statement(session, account_id)]


async def _balance(
    session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> ledger.Balance:
    async with session_factory() as session:
        return await ledger.balance(session, account_id)


async def _event(
    session_factory: async_sessionmaker[AsyncSession], event_id: str
) -> BillingProviderEvent:
    async with session_factory() as session:
        return (
            await session.execute(
                select(BillingProviderEvent).where(
                    BillingProviderEvent.provider == CREEM,
                    BillingProviderEvent.provider_event_id == event_id,
                )
            )
        ).scalar_one()


async def _refund(
    session_factory: async_sessionmaker[AsyncSession], adjustment: str
) -> BillingRefund | None:
    async with session_factory() as session:
        return (
            await session.execute(
                select(BillingRefund).where(
                    BillingRefund.provider == CREEM,
                    BillingRefund.provider_adjustment_id == adjustment,
                )
            )
        ).scalar_one_or_none()


async def _admin(*argv: str) -> int:
    """``scripts/billing_admin.py`` as the administrator runs it."""
    return await asyncio.to_thread(billing_admin.main, list(argv))


async def _audit(
    session_factory: async_sessionmaker[AsyncSession], entity_id: uuid.UUID
) -> list[tuple[str, str]]:
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingAuditEntry.action, BillingAuditEntry.actor).where(
                BillingAuditEntry.entity_id == entity_id
            )
        )
        return [tuple(r) for r in rows.tuples()]  # type: ignore[misc]


# ── The full path ──────────────────────────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestFullPath:
    async def test_checkout_purchase_refunds_dispute_lost(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        order = world.id("ord", "a")
        batch = await _bought(session_factory, world, order)

        # Checkout: the package's Creem product, our account and package.
        [sent] = world.sent
        assert str(sent.url) == "https://test-api.creem.io/v1/checkouts"
        assert json.loads(sent.content)["product_id"] == "prod_starter_test"
        assert world.metadata() == {
            "account_id": str(batch.account_id),
            "package_id": "starter",
        }
        # The purchase: a batch of provider creem, keyed by the order.
        assert (
            batch.units,
            batch.amount_paid_minor,
            batch.currency,
            batch.provider_transaction_id,
        ) == (1000, PAID, "EUR", order)
        assert (
            await _event(session_factory, world.id("evt", f"paid_{order}"))
        ).state == ("processed")

        # A refund the journal held (the administrator held 100 units under
        # the id Creem's dashboard gave the refund): approved, those units go.
        ours = world.id("ref", "ours")
        async with session_factory() as session:
            await refund_hold(
                session,
                account_id=batch.account_id,
                batch_id=batch.id,
                units=100,
                provider=CREEM,
                provider_adjustment_id=ours,
            )
            await session.commit()
        async with _client() as client:
            done = refund_created(world, "ours", ours, order, 119)
            assert (await _send(client, done)).status_code == 200

        # A refund made in Creem's dashboard: ceil(599 / 1198 x 1000) = 500.
        dashboard = world.id("ref", "dashboard")
        with capture_logs() as logs:
            async with _client() as client:
                made = refund_created(world, "dashboard", dashboard, order, 599)
                assert (await _send(client, made)).status_code == 200
        [warning] = [e for e in logs if e["event"] == "billing_refund_at_provider"]
        assert (warning["units_taken"], warning["units_lost"]) == (500, 0)
        refund = await _refund(session_factory, dashboard)
        assert refund is not None
        assert (refund.kind, refund.state, refund.units, refund.amount_minor) == (
            "refund",
            "approved",
            500,
            599,
        )

        # The units spent before the dispute stay spent.
        await _spend(session_factory, batch.account_id, 150)

        # The dispute: the 250 free units are held, the event processed.
        dispute = world.id("disp", "a")
        async with _client() as client:
            opened = dispute_created(world, "dispute", dispute, order)
            assert (await _send(client, opened)).status_code == 200
        event = await _event(session_factory, world.id("evt", "dispute"))
        assert (event.event_type, event.state, event.provider_adjustment_id) == (
            "chargeback",
            "processed",
            dispute,
        )
        held = await _refund(session_factory, dispute)
        assert held is not None and (held.kind, held.state, held.units) == (
            "dispute",
            "held",
            250,
        )
        before = await _balance(session_factory, batch.account_id)
        assert before.available == 0

        # Lost: the held units leave; no debt.
        assert await _admin("dispute-lost", "--dispute", dispute, "--actor", "ops") == 0
        assert await _journal(session_factory, batch.account_id) == [
            ("credit", 1000),
            ("refund_hold", 100),
            ("refund_completed", 100),
            ("refund_hold", 500),
            ("refund_completed", 500),
            ("debit", 150),
            ("refund_hold", 250),
            ("refund_completed", 250),
        ]
        after = await _balance(session_factory, batch.account_id)
        assert (after.available, after.difference) == (0, 0)
        assert await _audit(session_factory, held.id) == [("dispute.lost", "ops")]

        # Lost again: nothing changes; won after lost: refused.
        assert await _admin("dispute-lost", "--dispute", dispute, "--actor", "ops") == 0
        assert await _admin("dispute-won", "--dispute", dispute, "--actor", "ops") == 1
        assert len(await _audit(session_factory, held.id)) == 1

    async def test_a_won_dispute_frees_the_units(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        order = world.id("ord", "b")
        batch = await _bought(session_factory, world, order)
        dispute = world.id("disp", "b")
        async with _client() as client:
            opened = dispute_created(world, "dispute", dispute, order)
            assert (await _send(client, opened)).status_code == 200
            assert (await _send(client, opened)).status_code == 200  # a repeat
        assert (await _balance(session_factory, batch.account_id)).available == 0

        assert (
            await _admin(
                "dispute-won",
                "--dispute",
                dispute,
                "--provider",
                CREEM,
                "--actor",
                "ops",
            )
            == 0
        )
        assert (await _balance(session_factory, batch.account_id)).available == 1000
        held = await _refund(session_factory, dispute)
        assert held is not None and held.state == "rejected"
        assert await _audit(session_factory, held.id) == [("dispute.won", "ops")]


# ── Refunds made in Creem's dashboard ──────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestRefundAtProvider:
    async def test_a_resend_with_a_new_event_id_takes_nothing_more(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        order = world.id("ord", "c")
        batch = await _bought(session_factory, world, order)
        refund = world.id("ref", "c")
        async with _client() as client:
            first = refund_created(world, "first", refund, order, PAID)
            again = refund_created(world, "again", refund, order, PAID)  # new id
            assert (await _send(client, first)).status_code == 200
            assert (await _send(client, again)).status_code == 200
        assert await _journal(session_factory, batch.account_id) == [
            ("credit", 1000),
            ("refund_hold", 1000),
            ("refund_completed", 1000),
        ]
        assert (await _event(session_factory, world.id("evt", "again"))).state == (
            "processed"
        )

    async def test_what_is_no_longer_free_is_the_platforms_loss(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        order = world.id("ord", "d")
        batch = await _bought(session_factory, world, order)
        await _spend(session_factory, batch.account_id, 800)
        with capture_logs() as logs:
            async with _client() as client:
                full = refund_created(world, "full", world.id("ref", "d"), order, PAID)
                assert (await _send(client, full)).status_code == 200
        [warning] = [e for e in logs if e["event"] == "billing_refund_at_provider"]
        assert warning["log_level"] == "warning"
        assert (
            warning["units_wanted"],
            warning["units_taken"],
            warning["units_lost"],
        ) == (1000, 200, 800)
        balance = await _balance(session_factory, batch.account_id)
        assert (balance.available, balance.difference) == (0, 0)

    async def test_nothing_free_writes_no_refund(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        order = world.id("ord", "e")
        batch = await _bought(session_factory, world, order)
        await _spend(session_factory, batch.account_id, 1000)
        refund = world.id("ref", "e")
        async with _client() as client:
            done = refund_created(world, "spent", refund, order, PAID)
            assert (await _send(client, done)).status_code == 200
        assert await _refund(session_factory, refund) is None
        assert (await _event(session_factory, world.id("evt", "spent"))).state == (
            "processed"
        )

    async def test_a_refund_of_an_unknown_order_fails_and_is_sent_again(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        unknown = world.id("ord", "unknown")
        async with _client() as client:
            done = refund_created(world, "lost", world.id("ref", "f"), unknown, 100)
            assert (await _send(client, done)).status_code == 500
        event = await _event(session_factory, world.id("evt", "lost"))
        assert (event.state, event.error) == (
            "failed",
            f"BillingNotFoundError: no purchase batch of creem transaction {unknown}",
        )


# ── Disputes ───────────────────────────────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestDispute:
    async def test_with_every_unit_spent_nothing_is_held_and_no_debt_arises(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        order = world.id("ord", "g")
        batch = await _bought(session_factory, world, order)
        await _spend(session_factory, batch.account_id, 1000)
        dispute = world.id("disp", "g")
        async with _client() as client:
            opened = dispute_created(world, "dispute", dispute, order)
            assert (await _send(client, opened)).status_code == 200
        assert (await _event(session_factory, world.id("evt", "dispute"))).state == (
            "processed"
        )
        assert await _refund(session_factory, dispute) is None
        assert await _admin("dispute-lost", "--dispute", dispute, "--actor", "ops") == 1
        balance = await _balance(session_factory, batch.account_id)
        assert balance.difference == 0

    async def test_a_dispute_of_an_unknown_order_fails(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            opened = dispute_created(
                world, "orphan", world.id("disp", "h"), world.id("ord", "none")
            )
            assert (await _send(client, opened)).status_code == 500
        assert (await _event(session_factory, world.id("evt", "orphan"))).state == (
            "failed"
        )

    async def test_new_purchases_are_not_stopped(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        first = await _bought(session_factory, world, world.id("ord", "i1"))
        async with _client() as client:
            opened = dispute_created(
                world, "dispute", world.id("disp", "i"), world.id("ord", "i1")
            )
            assert (await _send(client, opened)).status_code == 200
        second = await _bought(session_factory, world, world.id("ord", "i2"))
        assert second.account_id == first.account_id
        balance = await _balance(session_factory, first.account_id)
        assert balance.available == 1000  # the new batch only


# ── The author's consent (operator 2026-11-12) ─────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestAuthorConsent:
    async def test_no_checkout_without_consent_to_the_terms_in_force(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        current = get_settings().billing_terms_version
        async with _client() as client:
            terms = await client.get(AUTHOR_TERMS)
            assert terms.json() == {
                "terms_version": current,
                "consent_required": True,
                "consented_at": None,
            }
            refused = await client.post(AUTHOR_CHECKOUT, json={"package_id": "starter"})
            assert refused.status_code == 409
            assert current in refused.json()["detail"]
            assert world.sent == []  # Creem was not asked

            agreed = await client.post(
                AUTHOR_CONSENT, json={"terms_version": current, "text_sha256": SHA}
            )
            assert agreed.status_code == 201
            again = await client.post(
                AUTHOR_CONSENT, json={"terms_version": current, "text_sha256": SHA}
            )
            assert again.status_code == 200
            assert again.json() == agreed.json()

            opened = await client.post(AUTHOR_CHECKOUT, json={"package_id": "starter"})
            assert opened.status_code == 200, opened.text
        async with session_factory() as session:
            consent = (
                await session.execute(
                    select(BillingConsent)
                    .join(
                        BillingAccount, BillingAccount.id == BillingConsent.account_id
                    )
                    .where(
                        BillingAccount.tenant_id == world.tenant_id,
                        BillingAccount.kind == "author",
                    )
                )
            ).scalar_one()
        assert (consent.channel, consent.terms_version) == ("api", current)
        assert world.metadata()["account_id"] == str(consent.account_id)

    async def test_a_new_revision_asks_again(self, world: World) -> None:
        current = get_settings().billing_terms_version
        async with _client() as client:
            agreed = await client.post(
                AUTHOR_CONSENT, json={"terms_version": current, "text_sha256": SHA}
            )
            assert agreed.status_code == 201
            newer = get_settings().model_copy(
                update={"billing_terms_version": "2099-01-01"}
            )
            with patch(
                "course_supporter.api.routes.billing.get_settings", return_value=newer
            ):
                terms = await client.get(AUTHOR_TERMS)
                assert terms.json()["consent_required"] is True
                refused = await client.post(
                    AUTHOR_CHECKOUT, json={"package_id": "starter"}
                )
                assert refused.status_code == 409
                stale = await client.post(
                    AUTHOR_CONSENT, json={"terms_version": current, "text_sha256": SHA}
                )
                assert stale.status_code == 409
                fresh = await client.post(
                    AUTHOR_CONSENT,
                    json={"terms_version": "2099-01-01", "text_sha256": SHA},
                )
                assert fresh.status_code == 201
                opened = await client.post(
                    AUTHOR_CHECKOUT, json={"package_id": "starter"}
                )
                assert opened.status_code == 200

    async def test_the_terms_need_the_prep_scope(self, world: World) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
            tenant_id=world.tenant_id,
            tenant_name="t",
            scopes=["check"],
            plan_id="basic",
            key_prefix="cs_test",
        )
        async with _client() as client:
            assert (await client.get(AUTHOR_TERMS)).status_code == 403
            consent = await client.post(
                AUTHOR_CONSENT,
                json={
                    "terms_version": get_settings().billing_terms_version,
                    "text_sha256": SHA,
                },
            )
            assert consent.status_code == 403


# ── The migration ──────────────────────────────────────────────────────


def test_the_migration_goes_down_and_up() -> None:
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    try:
        command.downgrade(cfg, "bilpay_payment_port")
        with engine.connect() as conn:
            refunds = {c["name"] for c in inspect(conn).get_columns("billing_refunds")}
            assert "kind" not in refunds
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        refunds = {c["name"] for c in inspect(conn).get_columns("billing_refunds")}
        assert "kind" in refunds
        channel = conn.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_billing_consents_channel'"
            )
        )
        assert "'api'" in str(channel)
    engine.dispose()


def test_the_downgrade_is_refused_while_a_dispute_is_held() -> None:
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    account_id, batch_id = uuid.uuid4(), uuid.uuid4()
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
                "'creem', :n, :n)"
            ),
            {"b": batch_id, "a": account_id, "n": f"ord_{account_id.hex[:12]}"},
        )
        conn.execute(
            text(
                "INSERT INTO billing_refunds (id, account_id, batch_id, kind, units, "
                "amount_minor, currency, provider, provider_adjustment_id) "
                "VALUES (:r, :a, :b, 'dispute', 10, 500, 'EUR', 'creem', :d)"
            ),
            {
                "r": uuid.uuid4(),
                "a": account_id,
                "b": batch_id,
                "d": f"disp_{account_id.hex[:12]}",
            },
        )
    try:
        with pytest.raises(RuntimeError, match="dispute row"):
            command.downgrade(cfg, "bilpay_payment_port")
    finally:
        with engine.begin() as conn:
            for table in ("billing_refunds", "billing_batches"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE account_id = :a"),  # noqa: S608
                    {"a": account_id},
                )
            conn.execute(
                text("DELETE FROM billing_accounts WHERE id = :a"), {"a": account_id}
            )
        engine.dispose()
