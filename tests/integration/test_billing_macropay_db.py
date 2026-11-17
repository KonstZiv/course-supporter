"""Macropay end to end on a live database, over HTTP (BIL-PAY, the second provider).

The Macropay adapter in its sandbox mode, its webhooks signed here by
Standard Webhooks (the bodies follow the documentation's fields, as in the unit
tests): ``order.paid`` → a batch of provider ``macropay``, its ``credit``
movement and its test mark (``marked_by = credit``) in one transaction; the
same delivery again, and the same order under another ``webhook-id``, credit
once; a partial refund completes the administrator's hold of the order once —
``refund.created`` and ``refund.updated`` of one refund, and ``order.refunded``,
take its units once.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_billing_catalog,
    get_payment_registry,
    get_session,
)
from course_supporter.billing import refunds
from course_supporter.billing.kinds import MarkOrigin, MovementKind, RefundState
from course_supporter.billing.payments.catalog import Catalog, Package
from course_supporter.billing.payments.macropay import (
    ID_HEADER,
    MACROPAY,
    SANDBOX_API_BASE,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    MacropayProvider,
)
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingBatch,
    BillingCheckout,
    BillingMovement,
    BillingProviderEvent,
    BillingRefund,
    BillingReservation,
    BillingTestBatch,
    Student,
    Tenant,
)

pytestmark = pytest.mark.requires_db

WEBHOOK = f"/api/v1/billing/webhooks/{MACROPAY}"
SECRET = "whsec_" + base64.b64encode(b"macropay-db-tests-only-32-bytes!").decode()
NOW = 1763380800
PAID = 1198
"""What the payer paid for the starter package: 990 EUR cents + 21 % VAT."""
CATALOG = Catalog(
    packages=(
        Package(id="starter", units=1000, prices_minor={"EUR": 990}, active=True),
    ),
    providers={MACROPAY: {"starter": "prod-starter-sandbox"}},
)
PROVIDER = MacropayProvider(
    api_key="macropay_oat_for_db_tests_only",
    webhook_secret=SECRET,
    api_base=SANDBOX_API_BASE,
    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    clock=lambda: NOW,
)


@dataclass
class World:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    token: str

    def id(self, name: str) -> str:
        """A ``webhook-id`` this test owns (its events are removed afterwards)."""
        return f"msg_{self.token}_{name}"


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[World]:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-macropay-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        student = Student(tenant_id=tenant.id, external_id="macropay-student")
        session.add(student)
        await session.flush()
        account = BillingAccount(
            kind="student", tenant_id=tenant.id, student_id=student.id
        )
        session.add(account)
        await session.commit()
    made = World(tenant.id, account.id, uuid.uuid4().hex[:12])

    yield made

    async with session_factory() as session:
        accounts = [made.account_id]
        batches = list(
            (
                await session.execute(
                    select(BillingBatch.id).where(BillingBatch.account_id.in_(accounts))
                )
            ).scalars()
        )
        await session.execute(
            delete(BillingProviderEvent).where(
                BillingProviderEvent.provider == MACROPAY,
                or_(
                    BillingProviderEvent.account_id.in_(accounts),
                    BillingProviderEvent.provider_event_id.startswith(made.id("")),
                ),
            )
        )
        await session.execute(
            delete(BillingMovement).where(BillingMovement.account_id.in_(accounts))
        )
        await session.execute(
            delete(BillingTestBatch).where(BillingTestBatch.batch_id.in_(batches))
        )
        for model, column in (
            (BillingRefund, BillingRefund.account_id),
            (BillingReservation, BillingReservation.account_id),
            (BillingCheckout, BillingCheckout.account_id),
            (BillingBatch, BillingBatch.account_id),
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
def routes(session_factory: async_sessionmaker[AsyncSession]) -> Iterator[None]:
    async def _session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_payment_registry] = lambda: ProviderRegistry(
        [PROVIDER]
    )
    app.dependency_overrides[get_billing_catalog] = lambda: CATALOG
    yield
    app.dependency_overrides.clear()


async def deliver(webhook_id: str, payload: dict[str, Any]) -> Response:
    """Macropay's delivery of ``payload``, signed (Standard Webhooks)."""
    body = json.dumps(payload).encode()
    headers = {
        ID_HEADER: webhook_id,
        TIMESTAMP_HEADER: str(NOW),
        SIGNATURE_HEADER: PROVIDER.sign(webhook_id, NOW, body),
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(WEBHOOK, content=body, headers=headers)


def order_paid(world: World, order: str) -> dict[str, Any]:
    return {
        "type": "order.paid",
        "timestamp": "2026-11-17T11:59:58Z",
        "data": {
            "id": order,
            "status": "paid",
            "net_amount": 990,
            "tax_amount": 208,
            "total_amount": PAID,
            "currency": "eur",
            "product_id": "prod-starter-sandbox",
            "metadata": {"account_id": str(world.account_id), "package_id": "starter"},
        },
    }


def refund_event(
    event_type: str, refund: str, order: str, amount: int, tax: int
) -> dict[str, Any]:
    return {
        "type": event_type,
        "timestamp": "2026-11-17T11:59:59Z",
        "data": {
            "id": refund,
            "order_id": order,
            "amount": amount,
            "tax_amount": tax,
            "currency": "eur",
            "status": "succeeded",
            "reason": "customer_request",
        },
    }


async def batches_of(
    session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> list[BillingBatch]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(BillingBatch).where(BillingBatch.account_id == account_id)
                )
            ).scalars()
        )


async def events_of(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> dict[str, str]:
    """``webhook-id`` → state of this test's events."""
    async with session_factory() as session:
        rows = await session.execute(
            select(
                BillingProviderEvent.provider_event_id, BillingProviderEvent.state
            ).where(
                BillingProviderEvent.provider == MACROPAY,
                BillingProviderEvent.provider_event_id.startswith(world.id("")),
            )
        )
        return dict(rows.tuples().all())


async def units_moved(
    session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    kind: MovementKind,
) -> int:
    async with session_factory() as session:
        found = await session.scalar(
            select(func.coalesce(func.sum(BillingMovement.units), 0)).where(
                BillingMovement.account_id == account_id,
                BillingMovement.kind == kind.value,
            )
        )
        return int(found or 0)


def _order() -> str:
    return str(uuid.uuid4())


@pytest.mark.usefixtures("routes")
class TestPurchase:
    async def test_order_paid_in_the_sandbox_credits_and_marks_together(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        assert PROVIDER.test_mode
        order = _order()

        response = await deliver(world.id("paid"), order_paid(world, order))

        assert response.status_code == 200, response.text
        [batch] = await batches_of(session_factory, world.account_id)
        assert (
            batch.provider,
            batch.units,
            batch.amount_paid_minor,
            batch.currency,
            batch.provider_transaction_id,
            batch.provider_line_id,
        ) == (MACROPAY, 1000, PAID, "EUR", order, order)
        async with session_factory() as session:
            [credit] = (
                await session.execute(
                    select(BillingMovement).where(
                        BillingMovement.batch_id == batch.id,
                        BillingMovement.kind == MovementKind.CREDIT.value,
                    )
                )
            ).scalars()
            mark = await session.get(BillingTestBatch, batch.id)
        assert credit.units == 1000
        assert mark is not None and mark.marked_by == MarkOrigin.CREDIT.value
        # One transaction, one instant: the batch, its credit and its mark.
        assert mark.marked_at == batch.created_at == credit.created_at
        assert await events_of(session_factory, world) == {
            world.id("paid"): "processed"
        }

    async def test_the_same_delivery_again_is_a_repeat(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        payload = order_paid(world, _order())

        first = await deliver(world.id("paid"), payload)
        again = await deliver(world.id("paid"), payload)

        assert (first.status_code, again.status_code) == (200, 200)
        assert len(await batches_of(session_factory, world.account_id)) == 1
        assert await events_of(session_factory, world) == {
            world.id("paid"): "processed"
        }
        assert (
            await units_moved(session_factory, world.account_id, MovementKind.CREDIT)
            == 1000
        )

    async def test_the_same_order_under_another_webhook_id_credits_once(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """A resend from the dashboard may come under a new ``webhook-id``
        (not verified): the journal knows the order."""
        payload = order_paid(world, _order())

        first = await deliver(world.id("paid"), payload)
        resent = await deliver(world.id("resent"), payload)

        assert (first.status_code, resent.status_code) == (200, 200)
        assert len(await batches_of(session_factory, world.account_id)) == 1
        assert await events_of(session_factory, world) == {
            world.id("paid"): "processed",
            world.id("resent"): "processed",
        }
        assert (
            await units_moved(session_factory, world.account_id, MovementKind.CREDIT)
            == 1000
        )


@pytest.mark.usefixtures("routes")
async def test_a_partial_refund_completes_the_hold_once(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Hold 600 units of the order, refund €5.99 of €11.98 at Macropay: 500
    units leave the batch, 100 are free again — once, whichever of the three
    events says so (``refund.created``, ``refund.updated``, ``order.refunded``)."""
    order, refund = _order(), str(uuid.uuid4())
    paid = await deliver(world.id("paid"), order_paid(world, order))
    assert paid.status_code == 200, paid.text
    async with session_factory() as session:
        await refunds.order_hold(session, provider=MACROPAY, order_id=order, units=600)
        await session.commit()

    created = await deliver(
        world.id("refund-created"),
        refund_event("refund.created", refund, order, amount=495, tax=104),
    )
    updated = await deliver(
        world.id("refund-updated"),
        refund_event("refund.updated", refund, order, amount=495, tax=104),
    )
    refunded = await deliver(
        world.id("order-refunded"),
        {
            "type": "order.refunded",
            "timestamp": "2026-11-17T12:00:00Z",
            "data": order_paid(world, order)["data"]
            | {"status": "partially_refunded", "refunded_amount": 495},
        },
    )

    assert [r.status_code for r in (created, updated, refunded)] == [200, 200, 200]
    assert (
        await units_moved(
            session_factory, world.account_id, MovementKind.REFUND_COMPLETED
        )
        == 500
    )  # ceil(599 x 1000 / 1198)
    assert (
        await units_moved(
            session_factory, world.account_id, MovementKind.REFUND_REJECTED
        )
        == 100
    )
    async with session_factory() as session:
        [row] = (
            await session.execute(
                select(BillingRefund).where(
                    BillingRefund.account_id == world.account_id
                )
            )
        ).scalars()
    assert (row.state, row.units, row.amount_minor, row.provider_adjustment_id) == (
        RefundState.APPROVED.value,
        500,
        599,
        refund,
    )
    assert await events_of(session_factory, world) == {
        world.id("paid"): "processed",
        world.id("refund-created"): "processed",
        world.id("refund-updated"): "processed",
        world.id("order-refunded"): "ignored",
    }
