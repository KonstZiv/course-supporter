"""BIL-E3, part D: a read creates no account; the first change creates one, audited.

Every read route — the billing GETs of the portal and of the author, the
estimate routes, and the portal's lists that ask who pays for a held review —
answers an owner WITHOUT an account as if an empty one existed, and leaves no
row in ``billing_accounts``. The first action that changes state creates the
account and, in the same transaction, one ``account.create`` audit entry: by
the administrator's ``--actor`` for the administrator's change, by
``"system"`` for a user's or the system's action. Two concurrent first actions
make one account and one entry.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_arq_redis,
    get_billing_catalog,
    get_current_student,
    get_current_tenant,
    get_payment_registry,
    get_s3_client,
)
from course_supporter.api.routes.public_numbers import current_numbers
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.billing import admin, audit, charging, held, outlook, quotes
from course_supporter.billing.accounts import (
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.consent import record_student_consent
from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import ActionKind, ConsentChannel, RuleScope
from course_supporter.billing.payments.catalog import Catalog, Package
from course_supporter.billing.payments.invoice import credit_invoice
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.config import get_settings
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingAccount,
    BillingAuditEntry,
    DocumentSummary,
    HomeworkSubmission,
    Student,
    StudentEnrollment,
    Tenant,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node
from tests._helpers.public_numbers import numbers_of, serve_numbers_of

pytestmark = pytest.mark.requires_db

ACTOR = "admin-bil-e3"
SHA = "e" * 64
CATALOG = Catalog(
    packages=(
        Package(id="starter", units=1000, prices_minor={"EUR": 990}, active=True),
    ),
    providers={},
)


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    course_id: uuid.UUID
    task_id: uuid.UUID
    test_id: uuid.UUID
    student_id: uuid.UUID
    held_id: uuid.UUID


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[World]:
    """A tenant, a course with a task and a test, an enrolled student with a
    review held for funds — and no billing account at all."""
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-e3-{uuid.uuid4().hex[:8]}")
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
        submission = HomeworkSubmission(
            tenant_id=tenant.id,
            student_id=student.id,
            course_node_id=course.id,
            node_id=course.id,
            authored_document_id=docs[0].id,
            file_url="s3://bucket/work.py",
            file_type="text/plain",
            original_filename="work.py",
            status="awaiting_funds",
        )
        session.add(submission)
        await session.commit()
        made = World(
            tenant.id, course.id, docs[0].id, docs[1].id, student.id, submission.id
        )
    yield made
    await drop_billing_of_tenant(session_factory, made.tenant_id)
    async with session_factory() as session:
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == made.tenant_id
            )
        )
        await session.commit()


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
    app.dependency_overrides[get_payment_registry] = lambda: ProviderRegistry([])
    app.dependency_overrides[get_billing_catalog] = lambda: CATALOG
    app.dependency_overrides[get_s3_client] = lambda: MagicMock()
    app.dependency_overrides[get_arq_redis] = lambda: AsyncMock()
    app.dependency_overrides[current_numbers] = serve_numbers_of(
        session_factory, world.tenant_id
    )
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def inserts(async_engine: AsyncEngine) -> Iterator[list[str]]:
    """Every INSERT into billing_accounts the engine sends, committed or not:
    a read route that inserted and rolled back would still show here."""
    seen: list[str] = []

    def spy(*args: Any) -> None:
        statement = str(args[2])
        if statement.lstrip().upper().startswith("INSERT INTO BILLING_ACCOUNTS"):
            seen.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", spy)
    yield seen
    event.remove(async_engine.sync_engine, "before_cursor_execute", spy)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _accounts(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(BillingAccount)
                .where(BillingAccount.tenant_id == world.tenant_id)
            )
            or 0
        )


async def _creations(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> list[tuple[BillingAccount, BillingAuditEntry]]:
    """Each account of the tenant with its ``account.create`` entries."""
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingAccount, BillingAuditEntry)
            .join(BillingAuditEntry, BillingAuditEntry.entity_id == BillingAccount.id)
            .where(
                BillingAccount.tenant_id == world.tenant_id,
                BillingAuditEntry.action == "account.create",
            )
        )
        return [(account, entry) for account, entry in rows.tuples()]


def _reads(world: World) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Every read route an owner without an account may call: (method, url, json)."""
    return [
        # The student's billing.
        ("GET", "/api/v1/portal/billing/terms", None),
        ("GET", "/api/v1/portal/billing/packages", None),
        ("GET", "/api/v1/portal/billing/checkouts/chk_unknown", None),
        ("GET", "/api/v1/portal/billing/balance", None),
        ("GET", "/api/v1/portal/billing/statement", None),
        ("GET", "/api/v1/portal/billing/outlook", None),
        (
            "GET",
            f"/api/v1/portal/billing/outlook?course_id={world.course_id}",
            None,
        ),
        ("POST", f"/api/v1/portal/tasks/{world.task_id}/estimate", None),
        ("POST", f"/api/v1/portal/tasks/{world.test_id}/estimate", None),
        # The portal's lists that ask who pays for a held review.
        ("GET", f"/api/v1/portal/tasks/{world.task_id}/submissions", None),
        ("GET", f"/api/v1/portal/submissions/{world.held_id}", None),
        ("GET", f"/api/v1/portal/courses/{world.course_id}/materials", None),
        # The author's billing.
        ("GET", "/api/v1/billing/terms", None),
        ("GET", "/api/v1/billing/packages", None),
        ("GET", "/api/v1/billing/checkouts/chk_unknown", None),
        ("GET", "/api/v1/billing/balance", None),
        ("GET", "/api/v1/billing/statement", None),
        ("GET", "/api/v1/billing/spending", None),
        ("GET", "/api/v1/billing/outlook", None),
        (
            "POST",
            f"/api/v1/nodes/{world.course_id}/documents/estimate",
            {"kind": "text", "text_chars": 20_000},
        ),
        ("POST", f"/api/v1/documents/{world.test_id}/reference/estimate", None),
    ]


async def _call_every_read(world: World) -> dict[str, tuple[int, Any]]:
    answers: dict[str, tuple[int, Any]] = {}
    async with _client() as client:
        for method, url, body in _reads(world):
            response = await client.request(method, url, json=body)
            answers[f"{method} {url}"] = (response.status_code, response.json())
    return answers


@pytest.mark.usefixtures("signed_in")
class TestReadsCreateNothing:
    async def test_every_read_answers_as_for_an_empty_account_and_creates_none(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        inserts: list[str],
    ) -> None:
        without = await _call_every_read(world)

        assert inserts == [], "a read tried to insert an account"
        assert await _accounts(session_factory, world) == 0, "a read made an account"
        for route, (status, _) in without.items():
            expected = 404 if "checkouts/chk_unknown" in route else 200
            assert status == expected, route

        # The same reads with both accounts there and empty: the same answers.
        async with session_factory() as session:
            await get_or_create_student_account(
                session, tenant_id=world.tenant_id, student_id=world.student_id
            )
            await get_or_create_author_account(session, tenant_id=world.tenant_id)
            await session.commit()
        with_empty = await _call_every_read(world)
        assert with_empty == without

    async def test_the_empty_values(self, world: World) -> None:
        answers = await _call_every_read(world)

        def body(method: str, url: str) -> Any:
            return answers[f"{method} {url}"][1]

        for who in ("/api/v1/portal/billing", "/api/v1/billing"):
            assert body("GET", f"{who}/balance") == {
                "balance": 0,
                "reserved": 0,
                "held": 0,
                "available": 0,
                "difference": 0,
                "next_expiry": None,
            }
            assert body("GET", f"{who}/statement") == {
                "items": [],
                "next_cursor": None,
            }
            terms = body("GET", f"{who}/terms")
            assert (terms["consent_required"], terms["consented_at"]) == (True, None)
            assert body("GET", f"{who}/packages")["purchase"] == {
                "open": False,
                "reason": "payment_unavailable",
            }
            assert body("GET", f"{who}/outlook")["expiring_soon"] is None
            assert body("GET", f"{who}/outlook")["charging_enabled"] is True
        student = body("GET", "/api/v1/portal/billing/outlook")
        assert (student["paid_by"], student["warning"]) == ("self", "next_may_fail")
        assert student["reviews_left"] == 0
        author = body("GET", "/api/v1/billing/outlook")
        assert (author["days_left"], author["warning"]) == (None, None)
        assert author["held_reviews"] == {
            "count": 0,
            "oldest_at": None,
            "missing_units": 0,
        }
        review = body("POST", f"/api/v1/portal/tasks/{world.task_id}/estimate")
        assert (review["available_units"], review["enough"]) == (0, False)
        assert review["missing_units"] == review["price_units"] > 0
        processing = body("POST", f"/api/v1/nodes/{world.course_id}/documents/estimate")
        assert (processing["available_units"], processing["enough"]) == (0, False)

    async def test_a_course_rule_is_read_without_the_students_account(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """The author pays under a rule: the rule's payer exists, the student's
        own account is still not created by the reads."""
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
        answers = await _call_every_read(world)

        outlook_ = answers[
            f"GET /api/v1/portal/billing/outlook?course_id={world.course_id}"
        ][1]
        assert outlook_["paid_by"] == "author"
        review = answers[f"POST /api/v1/portal/tasks/{world.task_id}/estimate"][1]
        assert (review["paid_by"], review["price_units"]) == ("author", None)
        author = answers["GET /api/v1/billing/outlook"][1]
        assert author["held_reviews"]["count"] == 1, "the held review is the author's"
        created = await _creations(session_factory, world)
        assert [account.kind for account, _ in created] == ["author"], (
            "only the rule's payer, created by the administrator's change"
        )

    async def test_the_read_functions_create_nothing_even_unflushed(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """In one session, before any commit: no INSERT reaches the table."""
        numbers = await numbers_of(session_factory, world.tenant_id)
        async with session_factory() as session:
            task = await session.get(AuthoredDocument, world.task_id)
            submission = await session.get(HomeworkSubmission, world.held_id)
            assert task is not None and submission is not None
            await quotes.review_quote(
                session,
                task=task,
                tenant_id=world.tenant_id,
                student_id=world.student_id,
            )
            await quotes.author_quote(
                session,
                action_kind=ActionKind.PROCESSING,
                tenant_id=world.tenant_id,
                course_node_id=world.course_id,
                e_usd=None,
            )
            await outlook.student_outlook(
                session,
                tenant_id=world.tenant_id,
                student_id=world.student_id,
                settings=get_settings(),
                numbers=numbers,
            )
            assert await held.waiting_for_author(session, [submission]) == frozenset()
            await admin.get_author_settings(session, world.tenant_id)
            count = await session.scalar(
                select(func.count())
                .select_from(BillingAccount)
                .where(BillingAccount.tenant_id == world.tenant_id)
            )
            assert count == 0
            assert not session.new


@pytest.mark.usefixtures("signed_in")
class TestTheFirstChangeCreates:
    @pytest.mark.parametrize("who", ["/api/v1/portal/billing", "/api/v1/billing"])
    async def test_a_consent_creates_one_account_audited_by_system(
        self, session_factory: async_sessionmaker[AsyncSession], world: World, who: str
    ) -> None:
        body = {
            "terms_version": get_settings().billing_terms_version,
            "text_sha256": SHA,
        }
        async with _client() as client:
            first = await client.post(f"{who}/consent", json=body)
            again = await client.post(f"{who}/consent", json=body)
        assert (first.status_code, again.status_code) == (201, 200)

        [(account, entry)] = await _creations(session_factory, world)
        assert await _accounts(session_factory, world) == 1
        assert account.kind == ("student" if "portal" in who else "author")
        assert (entry.actor, entry.entity, entry.before) == (
            audit.SYSTEM_ACTOR,
            "account",
            None,
        )
        assert entry.actor == "system"
        assert entry.after == audit.account_snapshot(account)
        assert entry.after is not None
        assert not {"tenant_id", "student_id"} & set(entry.after), "no owner"

    async def test_two_concurrent_first_changes_make_one_account_and_one_entry(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """The loser of the insert blocks on the unique index, then reads the
        winner's row — and writes no entry (it inserted nothing)."""
        async with session_factory() as first, session_factory() as second:
            won = await get_or_create_student_account(
                first, tenant_id=world.tenant_id, student_id=world.student_id
            )
            racing = asyncio.create_task(
                get_or_create_student_account(
                    second, tenant_id=world.tenant_id, student_id=world.student_id
                )
            )
            await asyncio.sleep(0.3)
            assert not racing.done(), "the second insert waits for the first"
            await first.commit()
            lost = await racing
            await second.commit()
        assert lost.id == won.id
        created = await _creations(session_factory, world)
        assert [account.id for account, _ in created] == [won.id]
        assert await _accounts(session_factory, world) == 1

    async def test_two_concurrent_consents_make_one_account_and_one_entry(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async def consent() -> None:
            async with session_factory() as session:
                await record_student_consent(
                    session,
                    tenant_id=world.tenant_id,
                    student_id=world.student_id,
                    terms_version="2026-11-09",
                    current_version="2026-11-09",
                    text_sha256=SHA,
                    channel=ConsentChannel.PORTAL,
                )
                await session.commit()

        await asyncio.gather(consent(), consent())

        assert await _accounts(session_factory, world) == 1
        assert len(await _creations(session_factory, world)) == 1


class TestTheActor:
    async def test_the_administrators_changes_carry_their_actor(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with session_factory() as session:
            await admin.set_test_purchase(
                session,
                tenant_id=world.tenant_id,
                student_id=world.student_id,
                allowed=True,
                actor=ACTOR,
            )
            await credit_invoice(
                session,
                tenant_id=world.tenant_id,
                units=100,
                amount_minor=1000,
                currency="EUR",
                invoice=f"INV-{uuid.uuid4().hex[:8]}",
                actor="admin-invoice",
            )
            await session.commit()

        actors = {
            account.kind: entry.actor
            for account, entry in await _creations(session_factory, world)
        }
        assert actors == {"student": ACTOR, "author": "admin-invoice"}

    async def test_an_administrators_creation_needs_an_actor(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        async with session_factory() as session:
            with pytest.raises(BillingInvalidError, match="actor"):
                await admin.owner_account(
                    session, tenant_id=world.tenant_id, create=True
                )
            assert (
                await admin.owner_account(
                    session, tenant_id=world.tenant_id, create=False
                )
                is None
            )
        assert await _accounts(session_factory, world) == 0

    async def test_the_start_of_a_paid_action_is_the_systems(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        """The run's payer (``charging.payer_of``) creates the student's account."""
        async with session_factory() as session:
            payer = await charging.payer_of(
                session,
                action_kind=ActionKind.REVIEW,
                tenant_id=world.tenant_id,
                course_node_id=world.course_id,
                student_id=world.student_id,
            )
            await session.commit()

        [(account, entry)] = await _creations(session_factory, world)
        assert account.id == payer.account_id
        assert entry.actor == "system"
