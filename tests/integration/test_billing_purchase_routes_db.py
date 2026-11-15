"""The routes of the purchase screens on a live database, over HTTP (BIL-PAY).

The fake provider (always in test mode) and Creem (its API replaced by an
``httpx.MockTransport``: nothing leaves the process) play the payment
provider. Every request commits like the deployed API; the rows a test wrote
are removed afterwards.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from course_supporter.billing import admin, ledger
from course_supporter.billing.accounts import (
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.expiry import expire_batches
from course_supporter.billing.journal import add_months
from course_supporter.billing.kinds import ActionKind, PayerSource
from course_supporter.billing.payments.catalog import Catalog, Package
from course_supporter.billing.payments.creem import (
    CREEM,
    LIVE_API_BASE,
    CreemProvider,
)
from course_supporter.billing.payments.fake import FAKE, FakeProvider
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.billing.refunds import (
    dispute_hold,
    dispute_lost,
    dispute_won,
    refund_approve,
    refund_hold,
    refund_reject,
)
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


# ── The checkout's number and state ────────────────────────────────────


def _page(world: World, who: str) -> str:
    """The billing page the payer comes back to."""
    settings = get_settings()
    if who == Who.AUTHOR:
        return f"{settings.author_app_base_url.rstrip('/')}/billing"
    return f"{settings.portal_base_url.rstrip('/')}/{world.tenant_id}/billing"


def _creem_paid(
    creem: CreemProvider, metadata: dict[str, Any], order: str
) -> tuple[dict[str, str], bytes]:
    """Creem's checkout.completed of a checkout with this metadata, signed."""
    body = json.dumps(
        {
            "id": f"evt_{order}",
            "eventType": "checkout.completed",
            "created_at": 1763200000000,
            "object": {
                "id": f"ch_{order}",
                "object": "checkout",
                "order": {
                    "id": order,
                    "amount_paid": 1198,
                    "currency": "EUR",
                    "status": "paid",
                },
                "units": 1,
                "metadata": metadata,
            },
        }
    ).encode()
    return {"creem-signature": creem.sign(body)}, body


async def _state(client: AsyncClient, who: str, ref: str) -> Response:
    return await client.get(f"{who}/checkouts/{ref}")


@pytest.mark.usefixtures("signed_in")
class TestCheckout:
    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_the_number_is_in_the_answer_the_return_path_and_the_metadata(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        who: str,
    ) -> None:
        creem = world.creem("https://test-api.creem.io/v1")
        world.providers[:] = [creem]
        async with _client() as client:
            await _consent(client, who)
            account_id = await _mark(session_factory, world, who)
            response = await _buy(client, who)
        assert response.status_code == 200, response.text
        answer = response.json()
        ref = answer["checkout_ref"]
        assert uuid.UUID(ref)
        [sent] = world.sent
        payload = json.loads(sent.content)
        assert payload["success_url"] == f"{_page(world, who)}/checkouts/{ref}"
        assert payload["metadata"] == {
            "account_id": str(account_id),
            "package_id": "starter",
            "checkout_ref": ref,
        }
        async with session_factory() as session:
            row = await session.get(BillingCheckout, uuid.UUID(ref))
        assert row is not None
        assert (row.account_id, row.package_id, row.provider) == (
            account_id,
            "starter",
            CREEM,
        )
        assert (row.provider_checkout_id, row.batch_id) == (answer["checkout_id"], None)

    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_pending_until_creems_checkout_completed_then_credited(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        who: str,
    ) -> None:
        creem = world.creem("https://test-api.creem.io/v1")
        world.providers[:] = [creem]
        async with _client() as client:
            await _consent(client, who)
            await _mark(session_factory, world, who)
            ref = (await _buy(client, who, "large")).json()["checkout_ref"]
            before = await _state(client, who, ref)
            metadata = json.loads(world.sent[0].content)["metadata"]
            headers, body = _creem_paid(creem, metadata, f"ord_{uuid.uuid4().hex[:12]}")
            paid = await client.post(
                f"/api/v1/billing/webhooks/{CREEM}", content=body, headers=headers
            )
            after = await _state(client, who, ref)
        assert before.status_code == 200
        assert before.json() == {
            "state": "pending",
            "package_id": "large",
            "units": None,
            "balance_after": None,
        }
        assert paid.status_code == 200
        assert after.json() == {
            "state": "credited",
            "package_id": "large",
            "units": 5000,
            "balance_after": 5000,
        }

    async def test_balance_after_is_what_is_available_now(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            await _consent(client, Who.STUDENT)
            await _mark(session_factory, world, Who.STUDENT)
            refs = []
            for package in ("starter", "large"):
                opened = (await _buy(client, Who.STUDENT, package)).json()
                refs.append(opened["checkout_ref"])
                headers, body = world.fake.purchase_webhook(opened["checkout_id"])
                assert (
                    await client.post(WEBHOOK, content=body, headers=headers)
                ).status_code == 200
            first, second = [await _state(client, Who.STUDENT, r) for r in refs]
        assert (first.json()["units"], first.json()["balance_after"]) == (1000, 6000)
        assert (second.json()["units"], second.json()["balance_after"]) == (5000, 6000)

    async def test_a_repeated_purchase_keeps_the_one_link(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            await _consent(client, Who.STUDENT)
            await _mark(session_factory, world, Who.STUDENT)
            opened = (await _buy(client, Who.STUDENT)).json()
            signed = world.fake.purchase_webhook(opened["checkout_id"])
            for _ in range(2):
                response = await client.post(
                    WEBHOOK, content=signed[1], headers=signed[0]
                )
                assert response.status_code == 200
            state = await _state(client, Who.STUDENT, opened["checkout_ref"])
        assert state.json()["state"] == "credited"
        assert state.json()["balance_after"] == 1000  # credited once

    @pytest.mark.parametrize(
        "ref",
        [str(uuid.uuid4()), "not-a-number", "0"],
        ids=["unknown", "not a number", "zero"],
    )
    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_an_unknown_checkout_is_404(self, who: str, ref: str) -> None:
        async with _client() as client:
            response = await _state(client, who, ref)
        assert response.status_code == 404
        assert response.json() == {
            "detail": {"code": "checkout_not_found", "details": "No such checkout."}
        }

    async def test_anothers_checkout_is_404_not_403(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            await _consent(client, Who.AUTHOR)
            await _mark(session_factory, world, Who.AUTHOR)
            ref = (await _buy(client, Who.AUTHOR)).json()["checkout_ref"]
            own = await _state(client, Who.AUTHOR, ref)
            by_the_student = await _state(client, Who.STUDENT, ref)
            app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
                tenant_id=uuid.uuid4(),
                tenant_name="another",
                scopes=["prep"],
                plan_id="basic",
                key_prefix="cs_test",
            )
            by_another_tenant = await _state(client, Who.AUTHOR, ref)
        assert own.status_code == 200
        for response in (by_the_student, by_another_tenant):
            assert (response.status_code, _code(response)) == (
                404,
                "checkout_not_found",
            )

    async def test_a_purchase_naming_anothers_checkout_is_credited_unlinked(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """The units go where the purchase says; the other checkout stays
        pending, and no account learns of another's checkout."""
        async with _client() as client:
            await _consent(client, Who.AUTHOR)
            await _consent(client, Who.STUDENT)
            await _mark(session_factory, world, Who.AUTHOR)
            student_id = await _mark(session_factory, world, Who.STUDENT)
            authors = (await _buy(client, Who.AUTHOR)).json()
            students = (await _buy(client, Who.STUDENT)).json()
            # The student's purchase comes back naming the author's checkout.
            opened = world.fake.checkouts[students["checkout_id"]]
            world.fake.checkouts[students["checkout_id"]] = dataclasses.replace(
                opened, checkout_ref=uuid.UUID(authors["checkout_ref"])
            )
            headers, body = world.fake.purchase_webhook(students["checkout_id"])
            paid = await client.post(WEBHOOK, content=body, headers=headers)
            author_state = await _state(client, Who.AUTHOR, authors["checkout_ref"])
            student_state = await _state(client, Who.STUDENT, students["checkout_ref"])
        assert paid.status_code == 200
        [batch] = await _batches(session_factory, student_id)
        assert batch.units == 1000
        assert author_state.json()["state"] == "pending"
        assert student_state.json()["state"] == "pending"

    async def test_the_checkout_state_needs_a_key_or_a_portal_session(self) -> None:
        app.dependency_overrides.pop(get_current_student)
        app.dependency_overrides.pop(get_current_tenant)
        ref = uuid.uuid4()
        async with _client() as client:
            portal = await _state(client, Who.STUDENT, str(ref))
            author = await _state(client, Who.AUTHOR, str(ref))
        assert (portal.status_code, author.status_code) == (401, 401)


# ── Balance and statement ──────────────────────────────────────────────


@dataclass
class Journal:
    """A known state of an author's journal (:func:`_author_journal`)."""

    account_id: uuid.UUID
    at: dict[str, datetime]
    actions: dict[str, str]


async def _author_journal(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> Journal:
    """An admin credit that expired and burned; a purchase; a charge from a
    reserve; a re-processing the platform covered; a reserve still active; a
    refund held; a charge beyond what was available (a difference); a second
    purchase that repaid it."""
    now = datetime.now(UTC)
    at = {
        "credit": add_months(now, -7),
        "bought": now - timedelta(days=10),
        "charged": now - timedelta(days=9),
        "platform": now - timedelta(days=9) + timedelta(hours=1),
        "reserved": now - timedelta(days=8),
        "held": now - timedelta(days=7),
        "short": now - timedelta(days=6),
        "bought_again": now - timedelta(days=5),
        "burned": now - timedelta(seconds=1),
    }
    token = uuid.uuid4().hex[:12]
    actions = {name: f"{name}-{token}" for name in ("a", "b", "c", "e")}
    async with session_factory() as session:
        account = await get_or_create_author_account(session, tenant_id=world.tenant_id)
        await ledger.credit_admin(
            session,
            account_id=account.id,
            units=100,
            reason="pilot",
            actor="tests",
            now=at["credit"],
        )
        first = await ledger.credit_purchase(
            session,
            account_id=account.id,
            units=1000,
            amount_paid_minor=990,
            currency="EUR",
            provider=FAKE,
            provider_transaction_id=f"txn_{token}_1",
            provider_line_id=f"line_{token}_1",
            now=at["bought"],
        )
        await ledger.reserve(
            session,
            action_kind=ActionKind.PROCESSING,
            action_key=actions["a"],
            account_id=account.id,
            units=50,
            now=at["charged"],
        )
        await ledger.settle(
            session,
            action_kind=ActionKind.PROCESSING,
            action_key=actions["a"],
            fact_units=30,
            beneficiary_account_id=account.id,
            payer_source=PayerSource.DEFAULT,
            now=at["charged"],
        )
        await ledger.settle(
            session,
            action_kind=ActionKind.PROCESSING,
            action_key=actions["e"],
            fact_units=15,
            beneficiary_account_id=account.id,
            payer_source=PayerSource.PLATFORM,
            account_id=account.id,
            now=at["platform"],
        )
        await ledger.reserve(
            session,
            action_kind=ActionKind.PROCESSING,
            action_key=actions["b"],
            account_id=account.id,
            units=10,
            now=at["reserved"],
        )
        await refund_hold(
            session,
            account_id=account.id,
            batch_id=first.batch.id,
            units=200,
            provider=FAKE,
            provider_adjustment_id=f"adj_{token}",
            now=at["held"],
        )
        await ledger.settle(
            session,
            action_kind=ActionKind.PROCESSING,
            action_key=actions["c"],
            fact_units=800,
            beneficiary_account_id=account.id,
            payer_source=PayerSource.DEFAULT,
            account_id=account.id,
            now=at["short"],
        )
        await ledger.credit_purchase(
            session,
            account_id=account.id,
            units=500,
            amount_paid_minor=500,
            currency="EUR",
            provider=FAKE,
            provider_transaction_id=f"txn_{token}_2",
            provider_line_id=f"line_{token}_2",
            now=at["bought_again"],
        )
        await session.commit()
        account_id = account.id
    async with session_factory() as session:
        await expire_batches(session, at["burned"])  # the admin credit burns
        await session.commit()
    return Journal(account_id=account_id, at=at, actions=actions)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


Item = tuple[str, bool, int, int, str | None]


def _items(response: Response) -> list[Item]:
    """(kind, affects_balance, units, balance_after, action id) of each line."""
    return [
        (
            item["kind"],
            item["affects_balance"],
            item["units"],
            item["balance_after"],
            item["action"]["id"] if item["action"] else None,
        )
        for item in response.json()["items"]
    ]


def _consistent(items: list[Item]) -> None:
    """Newest first: each line's balance is the older line's plus its units
    when it affects the balance, the older line's as it was otherwise."""
    for newer, older in itertools.pairwise(items):
        _, affects, units, after, _ = newer
        assert after == older[3] + (units if affects else 0), newer
        if not affects:
            assert units > 0, newer


@pytest.mark.usefixtures("signed_in")
class TestBalance:
    async def test_the_sums_against_a_known_journal(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        journal = await _author_journal(session_factory, world)
        async with _client() as client:
            response = await client.get(f"{Who.AUTHOR}/balance")
        assert response.status_code == 200
        # Batch 1: 1000 - 30 - 760 = 210 (10 reserved, 200 held); batch 2: 500
        # - 40 repaid = 460 free; the admin credit burned; the difference repaid.
        assert response.json() == {
            "balance": 670,
            "reserved": 10,
            "held": 200,
            "available": 460,
            "difference": 0,
            "next_expiry": {
                "units": 460,
                "expires_on": add_months(journal.at["bought_again"], 6)
                .date()
                .isoformat(),
            },
        }
        async with session_factory() as session:
            standing = await ledger.balance(session, journal.account_id)
        body = response.json()
        assert (
            standing.balance,
            standing.reserved,
            standing.held,
            standing.available,
            standing.difference,
        ) == tuple(
            body[k] for k in ("balance", "reserved", "held", "available", "difference")
        )

    @pytest.mark.parametrize(
        ("who", "difference"), [(Who.AUTHOR, 25), (Who.STUDENT, 0)], ids=PAYER_IDS
    )
    async def test_a_difference_is_an_authors_only(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        who: str,
        difference: int,
    ) -> None:
        async with session_factory() as session:
            account = (
                await get_or_create_author_account(session, tenant_id=world.tenant_id)
                if who == Who.AUTHOR
                else await get_or_create_student_account(
                    session,
                    tenant_id=world.tenant_id,
                    student_id=world.student.student_id,
                )
            )
            await ledger.settle(
                session,
                action_kind=ActionKind.REVIEW,
                action_key=f"owed-{uuid.uuid4().hex}",
                fact_units=25,
                beneficiary_account_id=account.id,
                payer_source=PayerSource.DEFAULT,
                account_id=account.id,
            )
            await session.commit()
            owed = (await ledger.balance(session, account.id)).difference
        async with _client() as client:
            response = await client.get(f"{who}/balance")
        assert owed == 25  # the journal's
        assert response.json() == {
            "balance": 0,
            "reserved": 0,
            "held": 0,
            "available": 0,
            "difference": difference,
            "next_expiry": None,
        }

    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_no_account_yet_is_zero_and_creates_none(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        who: str,
    ) -> None:
        async with _client() as client:
            balance = await client.get(f"{who}/balance")
            statement = await client.get(f"{who}/statement")
        assert balance.json() == {
            "balance": 0,
            "reserved": 0,
            "held": 0,
            "available": 0,
            "difference": 0,
            "next_expiry": None,
        }
        assert statement.json() == {"items": [], "next_cursor": None}
        assert await _accounts(session_factory, world) == []

    async def test_a_students_balance_is_the_students_own(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        await _author_journal(session_factory, world)  # the author's units
        async with _client() as client:
            response = await client.get(f"{Who.STUDENT}/balance")
        assert response.json()["balance"] == 0


@pytest.mark.usefixtures("signed_in")
class TestStatement:
    async def test_the_lines_newest_first_signed_with_the_balance_after(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        journal = await _author_journal(session_factory, world)
        a, c, e = (journal.actions[k] for k in ("a", "c", "e"))
        async with _client() as client:
            response = await client.get(f"{Who.AUTHOR}/statement")
        assert response.status_code == 200
        items = _items(response)
        assert items == [
            ("expired", True, -100, 670, None),
            ("difference_repaid", True, -40, 770, None),
            ("purchase", True, 500, 810, None),
            ("difference_arisen", False, 40, 310, c),
            ("charge", True, -760, 310, c),
            ("refund_hold", False, 200, 1070, None),
            ("platform_covered", False, 15, 1070, e),  # not a charge to the author
            ("charge", True, -30, 1070, a),
            ("purchase", True, 1000, 1100, None),
            ("admin_credit", True, 100, 100, None),
        ]
        _consistent(items)
        body = response.json()
        assert body["next_cursor"] is None
        # The newest line's balance is the balance now.
        async with _client() as client:
            balance = (await client.get(f"{Who.AUTHOR}/balance")).json()["balance"]
        assert body["items"][0]["balance_after"] == balance
        # Reserving and releasing are not lines; no money, no provider's id.
        assert "reserve" not in {item["kind"] for item in body["items"]}
        for item in body["items"]:
            assert set(item) == {
                "at",
                "kind",
                "affects_balance",
                "units",
                "balance_after",
                "action",
            }
        assert "txn_" not in response.text and "adj_" not in response.text
        charge = body["items"][-3]
        assert charge["action"] == {"kind": "processing", "id": a}
        assert datetime.fromisoformat(charge["at"]) == journal.at["charged"]

    async def test_pages_by_cursor_in_a_stable_order(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        await _author_journal(session_factory, world)
        async with _client() as client:
            whole = _items(await client.get(f"{Who.AUTHOR}/statement"))
            pages: list[list[Item]] = []
            cursor: str | None = None
            while True:
                params: dict[str, Any] = {"limit": 4}
                if cursor is not None:
                    params["cursor"] = cursor
                page = await client.get(f"{Who.AUTHOR}/statement", params=params)
                assert page.status_code == 200
                pages.append(_items(page))
                cursor = page.json()["next_cursor"]
                if cursor is None:
                    break
        assert [len(p) for p in pages] == [4, 4, 2]
        assert [line for page in pages for line in page] == whole

    async def test_a_period_is_from_inclusive_to_exclusive(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        journal = await _author_journal(session_factory, world)
        c = journal.actions["c"]
        async with _client() as client:
            period = await client.get(
                f"{Who.AUTHOR}/statement",
                params={
                    "from": _iso(journal.at["held"]),
                    "to": _iso(journal.at["bought_again"]),
                },
            )
            naive = await client.get(
                f"{Who.AUTHOR}/statement",
                params={
                    "from": journal.at["held"].replace(tzinfo=None).isoformat(),
                    "to": journal.at["bought_again"].replace(tzinfo=None).isoformat(),
                },
            )
        assert _items(period) == [
            ("difference_arisen", False, 40, 310, c),
            ("charge", True, -760, 310, c),
            ("refund_hold", False, 200, 1070, None),
        ]
        assert _items(naive) == _items(period)  # without a zone: UTC

    @pytest.mark.parametrize(
        "params",
        [{"limit": 0}, {"limit": 101}, {"from": "yesterday"}],
        ids=["limit 0", "limit 101", "not a date"],
    )
    async def test_bad_parameters_are_422(self, params: dict[str, Any]) -> None:
        async with _client() as client:
            response = await client.get(f"{Who.AUTHOR}/statement", params=params)
        assert response.status_code == 422

    @pytest.mark.parametrize("who", PAYERS, ids=PAYER_IDS)
    async def test_a_cursor_not_given_is_invalid_cursor(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        who: str,
    ) -> None:
        await _mark(session_factory, world, who)  # an account, so a statement
        async with _client() as client:
            response = await client.get(
                f"{who}/statement", params={"cursor": "not-a-cursor"}
            )
        assert (response.status_code, _code(response)) == (422, "invalid_cursor")

    async def test_each_holder_sees_their_own_lines(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        await _author_journal(session_factory, world)
        async with _client() as client:
            await _consent(client, Who.STUDENT)
            await _mark(session_factory, world, Who.STUDENT)
            opened = (await _buy(client, Who.STUDENT)).json()
            headers, body = world.fake.purchase_webhook(opened["checkout_id"])
            await client.post(WEBHOOK, content=body, headers=headers)
            student = await client.get(f"{Who.STUDENT}/statement")
        assert _items(student) == [("purchase", True, 1000, 1000, None)]


# ── Reason codes on the author's side ──────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestAuthorReasonCodes:
    async def test_terms_consent_required_then_terms_changed(self) -> None:
        async with _client() as client:
            purchase = await _buy(client, Who.AUTHOR)
            consent = await client.post(
                f"{Who.AUTHOR}/consent",
                json={"terms_version": "2000-01-01", "text_sha256": SHA},
            )
        assert (purchase.status_code, _code(purchase)) == (
            409,
            "terms_consent_required",
        )
        assert (consent.status_code, _code(consent)) == (409, "terms_changed")

    @pytest.mark.parametrize(
        ("body", "status", "code"),
        [
            ({"package_id": "nope"}, 404, "package_not_found"),
            ({"package_id": "retired"}, 404, "package_not_found"),
            (
                {"package_id": "starter", "provider": "paddle"},
                404,
                "provider_not_found",
            ),
        ],
    )
    async def test_nothing_on_sale(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        body: dict[str, str],
        status: int,
        code: str,
    ) -> None:
        async with _client() as client:
            await _consent(client, Who.AUTHOR)
            await _mark(session_factory, world, Who.AUTHOR)
            response = await client.post(f"{Who.AUTHOR}/checkout", json=body)
        assert (response.status_code, _code(response)) == (status, code)

    async def test_payment_unavailable(self, world: World) -> None:
        world.providers.clear()
        async with _client() as client:
            response = await _buy(client, Who.AUTHOR)
        assert (response.status_code, _code(response)) == (503, "payment_unavailable")

    async def test_provider_unavailable(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("no answer", request=request)

        world.providers[:] = [
            CreemProvider(
                api_key="creem_key_for_tests_only",
                webhook_secret="whsec_for_tests_only",
                api_base=LIVE_API_BASE,
                transport=httpx.MockTransport(down),
            )
        ]
        async with _client() as client:
            await _consent(client, Who.AUTHOR)
            response = await _buy(client, Who.AUTHOR)
        assert response.status_code == 502
        assert response.json() == {
            "detail": {
                "code": "provider_unavailable",
                "details": "The payment provider did not answer.",
            }
        }
        assert await _checkouts(session_factory, world) == 0  # nothing recorded


async def _checkouts(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> int:
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingCheckout.id)
            .join(BillingAccount, BillingAccount.id == BillingCheckout.account_id)
            .where(BillingAccount.tenant_id == world.tenant_id)
        )
        return len(rows.all())


@pytest.mark.usefixtures("signed_in")
class TestRefundsAndDisputesInTheStatement:
    async def test_one_refund_is_a_hold_and_one_outflow_and_a_lost_dispute_its_own(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """A refund held then approved: +100 held (no change), -100 out — never
        two minuses. A refund rejected: held, then freed. A dispute lost:
        dispute_lost, not refund. A dispute won: held, then freed."""
        start = datetime.now(UTC) - timedelta(days=3)

        def at(minutes: int) -> datetime:
            return start + timedelta(minutes=minutes)

        token = uuid.uuid4().hex[:12]
        async with session_factory() as session:
            account = await get_or_create_student_account(
                session, tenant_id=world.tenant_id, student_id=world.student.student_id
            )
            first = await ledger.credit_purchase(
                session,
                account_id=account.id,
                units=1000,
                amount_paid_minor=990,
                currency="EUR",
                provider=FAKE,
                provider_transaction_id=f"txn_{token}_1",
                provider_line_id=f"line_{token}_1",
                now=at(1),
            )
            for adjustment, units, minute in ((f"adj_{token}_a", 100, 2),):
                await refund_hold(
                    session,
                    account_id=account.id,
                    batch_id=first.batch.id,
                    units=units,
                    provider=FAKE,
                    provider_adjustment_id=adjustment,
                    now=at(minute),
                )
            await refund_approve(
                session,
                provider=FAKE,
                provider_adjustment_id=f"adj_{token}_a",
                now=at(3),
            )
            await refund_hold(
                session,
                account_id=account.id,
                batch_id=first.batch.id,
                units=50,
                provider=FAKE,
                provider_adjustment_id=f"adj_{token}_b",
                now=at(4),
            )
            await refund_reject(
                session,
                provider=FAKE,
                provider_adjustment_id=f"adj_{token}_b",
                now=at(5),
            )
            await dispute_hold(
                session,
                provider=FAKE,
                dispute_id=f"dsp_{token}_1",
                transaction_id=f"txn_{token}_1",
                now=at(6),
            )
            await dispute_lost(
                session, provider=FAKE, dispute_id=f"dsp_{token}_1", now=at(7)
            )
            await ledger.credit_purchase(
                session,
                account_id=account.id,
                units=500,
                amount_paid_minor=500,
                currency="EUR",
                provider=FAKE,
                provider_transaction_id=f"txn_{token}_2",
                provider_line_id=f"line_{token}_2",
                now=at(8),
            )
            await dispute_hold(
                session,
                provider=FAKE,
                dispute_id=f"dsp_{token}_2",
                transaction_id=f"txn_{token}_2",
                now=at(9),
            )
            await dispute_won(
                session, provider=FAKE, dispute_id=f"dsp_{token}_2", now=at(10)
            )
            await session.commit()
        async with _client() as client:
            statement = await client.get(f"{Who.STUDENT}/statement")
            balance = await client.get(f"{Who.STUDENT}/balance")
        items = _items(statement)
        assert items == [
            ("dispute_released", False, 500, 500, None),
            ("dispute_hold", False, 500, 500, None),
            ("purchase", True, 500, 500, None),
            ("dispute_lost", True, -900, 0, None),
            ("dispute_hold", False, 900, 900, None),
            ("refund_released", False, 50, 900, None),
            ("refund_hold", False, 50, 900, None),
            ("refund", True, -100, 900, None),
            ("refund_hold", False, 100, 1000, None),
            ("purchase", True, 1000, 1000, None),
        ]
        _consistent(items)
        assert balance.json()["balance"] == items[0][3] == 500
        assert "dsp_" not in statement.text and "adj_" not in statement.text
