"""BIL-E parts B and C over HTTP, on a live database: prices, 402, reminders.

Numbers: K = 1 700, margin 0.20 (``config/cost_estimate.yaml``). Charging is
switched on and the path's test mode off for each test, unless a test says
otherwise; every world is a fresh tenant.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_arq_redis,
    get_current_student,
    get_current_tenant,
    get_s3_client,
)
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.billing import admin, charging, ledger, quotes
from course_supporter.billing.accounts import (
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.journal import add_months
from course_supporter.billing.kinds import ActionKind, PayerSource, RuleScope
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    DocumentSummary,
    HomeworkSubmission,
    Student,
    StudentEnrollment,
    Tenant,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

ACTOR = "test-admin"
WORK = b"def add(a, b):\n    return a + b\n" * 40


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    course_id: uuid.UUID
    task_id: uuid.UUID
    test_id: uuid.UUID
    student_id: uuid.UUID
    student: uuid.UUID
    author: uuid.UUID


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[World]:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-e-routes-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        course = make_root_course_node(tenant_id=tenant.id, title="Курс", order=0)
        session.add(course)
        await session.flush()
        docs = [
            AuthoredDocument(
                course_node_id=course.id,
                course_root_id=course.id,
                source_type="text",
                source_url=f"https://example.com/{uuid.uuid4().hex}",
                task_type=task_type,
                language="ukr",
                content_hash=uuid.uuid4().hex * 2,
            )
            for task_type in ("task", "test")
        ]
        student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:8]}")
        session.add_all([*docs, student])
        await session.flush()
        for doc in docs:
            session.add(
                DocumentSummary(
                    authored_document_id=doc.id,
                    course_root_id=course.id,
                    title="Завдання",
                    status="ready",
                )
            )
        session.add(StudentEnrollment(student_id=student.id, course_node_id=course.id))
        student_account = await get_or_create_student_account(
            session, tenant_id=tenant.id, student_id=student.id
        )
        author_account = await get_or_create_author_account(
            session, tenant_id=tenant.id
        )
        await session.commit()
        made = World(
            tenant.id,
            course.id,
            docs[0].id,
            docs[1].id,
            student.id,
            student_account.id,
            author_account.id,
        )
    yield made
    await drop_billing_of_tenant(session_factory, made.tenant_id)


@pytest.fixture(autouse=True)
def _charging_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: True)
    monkeypatch.setattr(quotes, "in_test_mode", lambda *a, **k: False)


@pytest.fixture()
def signed_in(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> Iterator[None]:
    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_student] = lambda: StudentContext(
        student_id=world.student_id,
        tenant_id=world.tenant_id,
        login="s",
        display_name=None,
    )
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
        tenant_id=world.tenant_id,
        tenant_name="t",
        scopes=["prep"],
        plan_id="basic",
        key_prefix="cs_test",
    )
    app.dependency_overrides[get_s3_client] = lambda: MagicMock()
    app.dependency_overrides[get_arq_redis] = lambda: AsyncMock()
    yield
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def buy(
    session_factory: async_sessionmaker[AsyncSession],
    account: uuid.UUID,
    units: int,
    *,
    at: datetime | None = None,
) -> uuid.UUID:
    async with session_factory() as session:
        done = await ledger.credit_purchase(
            session,
            account_id=account,
            units=units,
            amount_paid_minor=500,
            currency="EUR",
            provider="fake",
            now=at,
        )
        await session.commit()
        return done.batch.id


async def course_rule(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> None:
    async with session_factory() as session:
        await admin.create_payment_rule(
            session,
            actor=ACTOR,
            tenant_id=world.tenant_id,
            scope=RuleScope.COURSE,
            action_kind=ActionKind.REVIEW,
            course_node_id=world.course_id,
        )
        await session.commit()


# ── B1: a student's review ────────────────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestReviewEstimate:
    async def test_typical_and_file_basis(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        await buy(session_factory, world.student, 10_000)
        async with _client() as client:
            typical = await client.post(
                f"/api/v1/portal/tasks/{world.task_id}/estimate"
            )
            with_file = await client.post(
                f"/api/v1/portal/tasks/{world.task_id}/estimate",
                files={"file": ("work.py", WORK, "text/x-python")},
            )

        assert typical.status_code == 200, typical.text
        body = typical.json()
        assert body["basis"] == "typical"
        assert body["paid_by"] == "self"
        assert body["charging_enabled"] is True
        assert body["price_units"] > 0
        assert body["available_units"] == 10_000
        assert (body["enough"], body["missing_units"]) == (True, 0)
        assert with_file.status_code == 200, with_file.text
        assert with_file.json()["basis"] == "file"
        assert with_file.json()["price_units"] >= body["price_units"], (
            "a file is priced on its own size, more than the prompts alone"
        )

    async def test_short_says_what_is_missing(self, world: World) -> None:
        async with _client() as client:
            body = (
                await client.post(f"/api/v1/portal/tasks/{world.task_id}/estimate")
            ).json()

        assert body["enough"] is False
        assert body["available_units"] == 0
        assert body["missing_units"] == body["price_units"] > 0

    async def test_the_author_pays_no_numbers_of_the_author(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        await course_rule(session_factory, world)
        async with _client() as client:
            body = (
                await client.post(f"/api/v1/portal/tasks/{world.task_id}/estimate")
            ).json()

        assert body["paid_by"] == "author"
        assert body["available_units"] is None
        assert body["missing_units"] is None
        assert body["enough"] is True, "the author's units never stop a review"

    async def test_the_same_work_costs_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def same(*_: Any, **__: Any) -> bool:
            return True

        monkeypatch.setattr(quotes, "_same_work", same)
        async with _client() as client:
            body = (
                await client.post(
                    f"/api/v1/portal/tasks/{world.task_id}/estimate",
                    files={"file": ("work.py", WORK, "text/x-python")},
                )
            ).json()

        assert (body["price_units"], body["enough"]) == (0, True)

    async def test_a_test_is_free_with_a_balance_and_closed_without(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        url = f"/api/v1/portal/tasks/{world.test_id}/estimate"
        async with _client() as client:
            empty = (await client.post(url)).json()
            await buy(session_factory, world.student, 1)
            funded = (await client.post(url)).json()

        assert (empty["price_units"], empty["enough"], empty["missing_units"]) == (
            0,
            False,
            1,
        )
        assert (funded["price_units"], funded["enough"]) == (0, True)

    async def test_charging_off_or_test_mode_refuses_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(quotes, "in_test_mode", lambda *a, **k: True)
        async with _client() as client:
            test_mode = (
                await client.post(f"/api/v1/portal/tasks/{world.task_id}/estimate")
            ).json()
        monkeypatch.setattr(quotes, "in_test_mode", lambda *a, **k: False)
        monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: False)
        async with _client() as client:
            off = (
                await client.post(f"/api/v1/portal/tasks/{world.task_id}/estimate")
            ).json()

        for body in (test_mode, off):
            assert body["charging_enabled"] is False
            assert body["enough"] is True
            assert body["price_units"] > 0, "the price is still shown"


# ── B3: the refusal ───────────────────────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestRefusal:
    async def test_a_student_short_of_the_price_gets_402_and_nothing_is_stored(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            response = await client.post(
                f"/api/v1/portal/tasks/{world.task_id}/submissions",
                files={"file": ("work.py", WORK, "text/x-python")},
            )

        assert response.status_code == 402, response.text
        body = response.json()
        assert body["reason"] == "insufficient_units"
        assert isinstance(body["detail"], str)
        assert body["available_units"] == 0
        assert body["missing_units"] == body["price_units"] > 0
        async with session_factory() as session:
            stored = await session.scalar(
                HomeworkSubmission.__table__.select()
                .where(HomeworkSubmission.student_id == world.student_id)
                .with_only_columns(HomeworkSubmission.id)
            )
        assert stored is None

    async def test_the_author_paying_never_gets_the_student_refused(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        from course_supporter.api.routes.portal_submissions import _require_units

        await course_rule(session_factory, world)
        async with session_factory() as session:
            task = await session.get(AuthoredDocument, world.task_id)
            assert task is not None
            await _require_units(
                session,
                task,
                StudentContext(
                    student_id=world.student_id,
                    tenant_id=world.tenant_id,
                    login="s",
                    display_name=None,
                ),
                file=None,
            )  # the author has 0 units: no refusal

    async def test_off_nothing_is_refused(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from course_supporter.api.routes.portal_submissions import _require_units

        monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: False)
        async with session_factory() as session:
            task = await session.get(AuthoredDocument, world.task_id)
            assert task is not None
            await _require_units(
                session,
                task,
                StudentContext(
                    student_id=world.student_id,
                    tenant_id=world.tenant_id,
                    login="s",
                    display_name=None,
                ),
                file=None,
            )


# ── B2: an author's own action ────────────────────────────────────────


@pytest.mark.usefixtures("signed_in")
class TestAuthorEstimate:
    async def test_processing_without_credit_is_short_with_credit_enough(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        url = f"/api/v1/nodes/{world.course_id}/documents/estimate"
        payload = {"kind": "text", "text_chars": 20_000}
        async with _client() as client:
            short = await client.post(url, json=payload)
            async with session_factory() as session:
                await admin.update_author_settings(
                    session, world.tenant_id, actor=ACTOR, credit_allowed=True
                )
                await session.commit()
            credit = await client.post(url, json=payload)

        assert short.status_code == 200, short.text
        body = short.json()
        assert set(body) == {
            "price_units",
            "available_units",
            "enough",
            "missing_units",
            "charging_enabled",
        }
        assert body["price_units"] > 0
        assert (body["enough"], body["missing_units"]) == (False, body["price_units"])
        assert credit.json()["enough"] is True

    async def test_key_explanations_of_a_foreign_task_are_404(
        self, world: World
    ) -> None:
        async with _client() as client:
            response = await client.post(
                f"/api/v1/documents/{uuid.uuid4()}/reference/estimate"
            )
        assert response.status_code == 404

    async def test_key_explanations_are_priced(self, world: World) -> None:
        async with _client() as client:
            response = await client.post(
                f"/api/v1/documents/{world.test_id}/reference/estimate"
            )
        assert response.status_code == 200, response.text
        assert response.json()["price_units"] > 0


# ── C: reminders ──────────────────────────────────────────────────────


async def charged_reviews(
    session_factory: async_sessionmaker[AsyncSession],
    account: uuid.UUID,
    beneficiary: uuid.UUID,
    units: list[int],
) -> None:
    """Reviews charged to the account, without a reserve (settle with a payer)."""
    async with session_factory() as session:
        for charge in units:
            await ledger.settle(
                session,
                action_kind=ActionKind.REVIEW,
                action_key=str(uuid.uuid4()),
                fact_units=charge,
                beneficiary_account_id=beneficiary,
                payer_source=PayerSource.DEFAULT,
                account_id=account,
            )
        await session.commit()


@pytest.mark.usefixtures("signed_in")
class TestStudentOutlook:
    async def test_reviews_left_from_the_average_and_the_warnings(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        await buy(session_factory, world.student, 500)
        await charged_reviews(session_factory, world.student, world.student, [100, 100])
        async with _client() as client:
            three = (await client.get("/api/v1/portal/billing/outlook")).json()
            await charged_reviews(
                session_factory, world.student, world.student, [100, 100]
            )
            one = (await client.get("/api/v1/portal/billing/outlook")).json()
            await charged_reviews(session_factory, world.student, world.student, [100])
            none = (await client.get("/api/v1/portal/billing/outlook")).json()

        assert (three["reviews_left"], three["warning"], three["paid_by"]) == (
            3,
            None,
            "self",
        )
        assert (one["reviews_left"], one["warning"]) == (1, "low")
        assert (none["reviews_left"], none["warning"]) == (0, "next_may_fail")

    async def test_without_history_nothing_known_says_nothing(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        await buy(session_factory, world.student, 500)
        async with _client() as client:
            body = (await client.get("/api/v1/portal/billing/outlook")).json()
        assert (body["reviews_left"], body["warning"]) == (None, None)

    async def test_the_author_paying_hides_the_numbers(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        await course_rule(session_factory, world)
        async with _client() as client:
            body = (
                await client.get(
                    "/api/v1/portal/billing/outlook",
                    params={"course_id": str(world.course_id)},
                )
            ).json()
        assert body == {
            "reviews_left": None,
            "warning": None,
            "expiring_soon": None,
            "paid_by": "author",
        }

    async def test_units_expiring_within_14_days(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        now = datetime.now(UTC)
        async with _client() as client:
            await buy(session_factory, world.student, 30)  # expires in 6 months
            later = (await client.get("/api/v1/portal/billing/outlook")).json()
            # Bought so that it expires in 10 days (a batch lasts 6 months).
            await buy(
                session_factory,
                world.student,
                40,
                at=add_months(now + timedelta(days=10), -6),
            )
            soon = (await client.get("/api/v1/portal/billing/outlook")).json()

        assert later["expiring_soon"] is None
        assert soon["expiring_soon"] == {
            "units": 40,
            "expires_on": (now + timedelta(days=10)).date().isoformat(),
        }


@pytest.mark.usefixtures("signed_in")
class TestAuthorOutlook:
    async def test_days_left_at_the_weeks_pace(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with _client() as client:
            idle = (await client.get("/api/v1/billing/outlook")).json()
            await buy(session_factory, world.author, 1000)
            await charged_reviews(session_factory, world.author, world.student, [140])
            ok = (await client.get("/api/v1/billing/outlook")).json()
            await charged_reviews(session_factory, world.author, world.student, [700])
            low = (await client.get("/api/v1/billing/outlook")).json()

        assert idle == {"days_left": None, "warning": None, "expiring_soon": None}
        assert (ok["days_left"], ok["warning"]) == (43, None)  # 860 / (140/7)
        assert (low["days_left"], low["warning"]) == (1, "runs_out_soon")  # 160/120


def test_the_new_billing_routes() -> None:
    """Parts B and C add these routes and no other."""
    paths = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        and ("estimate" in route.path or "outlook" in route.path)
        for method in route.methods
    }
    assert paths == {
        ("/api/v1/portal/tasks/{authored_document_id}/estimate", "POST"),
        ("/api/v1/nodes/{node_id}/documents/estimate", "POST"),
        ("/api/v1/documents/{document_id}/reference/estimate", "POST"),
        ("/api/v1/portal/billing/outlook", "GET"),
        ("/api/v1/billing/outlook", "GET"),
    }
