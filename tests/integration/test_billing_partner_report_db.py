"""Б-3: the partner's report, the provider's figures of a purchase, the rate (live DB).

The locks are named by the letters of the task, transliterated in the test
names (``test_a_…``, ``test_b_…``, ``test_v_…``, ``test_g_…``, ``test_gg_…``,
``test_d_…``, ``test_e_…``, ``test_ye_…``, ``test_zh_…``):

(а) the purchase's webhook and ``creem-sync`` write the tax, the refunded part
    and the fee (``billing_batch_provider_figures``) — Creem's API replaced by
    an ``httpx.MockTransport``; the fee by the fake provider, since Creem gives
    none; a repeat changes nothing; ``--dry-run`` writes nothing;
(б) Creem's API without a fee: the figures' ``fee_minor`` stays empty; in a
    currency Creem's tariff has no fixed part for (EUR) the report says
    «комісія Creem: тариф для EUR не задано»;
(в) the tax in proportion to the units spent, from two batches of different
    price and tax; a batch without Creem's data is counted «без даних Creem»;
(г) the rate: set, changed, removed, refused out of range; audited; the share
    is rate x net; without a rate, no sum;
(ґ) service students left out; another school never in; two currencies apart;
(д) the month's refunds on a line apart; the month's edges;
(е) the migration down and up; one head;
(є) the batch stays insert-only, as in main: an UPDATE of any of its columns,
    the amount among them, is refused by the trigger; the provider's figures
    are upserted beside it, allowed and idempotent (Б-5);
(ж) Creem's fee by its tariff (Б-5): two USD batches — the formula; an EUR
    batch — «тариф для EUR не задано»; a fee from the API has precedence.

The journal is written by the ledger itself at fixed instants, so the periods
are exact whatever day the test runs on; batches are taken in the order they
expire, so a batch bought earlier pays first.

Requires ``docker compose up -d``; run with ``--run-db``.
"""  # noqa: RUF002 — the task's lock letters

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from scripts import billing_admin
from sqlalchemy import create_engine, delete, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.billing import ledger, partner_report, provider_figures
from course_supporter.billing.accounts import (
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import ActionKind, AuditAction, PayerSource
from course_supporter.billing.payments.catalog import (
    Catalog,
    Package,
    get_catalog,
    load_catalog,
)
from course_supporter.billing.payments.creem import (
    CREEM,
    SIGNATURE_HEADER,
    CreemProvider,
)
from course_supporter.billing.payments.fake import PURCHASE, FakeProvider
from course_supporter.billing.payments.port import TransactionFigures
from course_supporter.billing.payments.webhooks import handle_events
from course_supporter.billing.refunds import refund_at_provider
from course_supporter.config import get_settings
from course_supporter.cost_kinds import CostKind, TariffWindow
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingAuditEntry,
    BillingAuthorSettings,
    BillingBatch,
    BillingBatchProviderFigures,
    BillingProviderEvent,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

Factory = async_sessionmaker[AsyncSession]

OCTOBER = date(2026, 10, 1)
IN_OCTOBER = datetime(2026, 10, 15, 12, tzinfo=UTC)
SEP_1 = datetime(2026, 9, 1, tzinfo=UTC)
SEP_2 = datetime(2026, 9, 2, tzinfo=UTC)
SEP_3 = datetime(2026, 9, 3, tzinfo=UTC)
SYNC_DAY = datetime(2026, 3, 2, 10, tzinfo=UTC)
"""Batches the sync tests ask about: a day no other test buys on."""

TEST_KEY = "creem_test_key_for_db_tests_only"
TEST_SECRET = "whsec_test_secret_for_db_tests_only"
SHA = "b" * 64
CATALOG = Catalog(
    packages=(
        Package(id="starter", units=1000, prices_minor={"EUR": 1000}, active=True),
    ),
    providers={CREEM: {"starter": "prod_starter_test"}},
)
SHIPPED_TARIFFS = load_catalog(Path("config/billing_packages.yaml")).provider_fees
"""The fee tariffs as shipped (``provider_fees``): Creem 3.9 % + USD 40, EUR not set."""


# ── A school ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class School:
    tenant_id: uuid.UUID
    name: str
    author_account: uuid.UUID
    courses: dict[str, tuple[uuid.UUID, uuid.UUID]]
    """Title → (root node, its homework task)."""
    students: dict[str, uuid.UUID]
    accounts: dict[str, uuid.UUID]


async def _make_school(
    factory: Factory, courses: list[str], students: dict[str, bool]
) -> School:
    """A tenant, its courses (a task each), its students (``True``: a service
    student) and their accounts."""
    async with factory() as session:
        tenant = Tenant(name=f"b3-partner-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        nodes = {
            title: make_root_course_node(tenant_id=tenant.id, title=title, order=i)
            for i, title in enumerate(courses)
        }
        session.add_all(nodes.values())
        await session.flush()
        tasks = {
            title: AuthoredDocument(
                course_node_id=node.id,
                course_root_id=node.id,
                source_type="text",
                source_url=f"https://example.com/{uuid.uuid4().hex}",
                title=f"Завдання {title}",
                task_type="task",
            )
            for title, node in nodes.items()
        }
        session.add_all(tasks.values())
        rows = {
            key: Student(
                tenant_id=tenant.id,
                external_id=f"{key}-{uuid.uuid4().hex[:8]}",
                display_name=key,
                is_test_student=service,
            )
            for key, service in students.items()
        }
        session.add_all(rows.values())
        await session.flush()
        author = await get_or_create_author_account(session, tenant_id=tenant.id)
        accounts = {
            key: (
                await get_or_create_student_account(
                    session, tenant_id=tenant.id, student_id=student.id
                )
            ).id
            for key, student in rows.items()
        }
        await session.commit()
        return School(
            tenant_id=tenant.id,
            name=tenant.name,
            author_account=author.id,
            courses={t: (nodes[t].id, tasks[t].id) for t in nodes},
            students={key: row.id for key, row in rows.items()},
            accounts=accounts,
        )


async def _drop_school(factory: Factory, school: School) -> None:
    async with factory() as session:
        jobs = select(Job.id).where(Job.tenant_id == school.tenant_id)
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id.in_(jobs))
        )
        await session.execute(delete(Job).where(Job.tenant_id == school.tenant_id))
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == school.tenant_id
            )
        )
        await session.commit()
    await drop_billing_of_tenant(factory, school.tenant_id)
    async with factory() as session:
        roots = [node for node, _ in school.courses.values()]
        await session.execute(
            delete(AuthoredDocument).where(AuthoredDocument.course_root_id.in_(roots))
        )
        await session.execute(delete(CourseNode).where(CourseNode.id.in_(roots)))
        await session.execute(
            delete(Student).where(Student.tenant_id == school.tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == school.tenant_id))
        await session.commit()


@pytest.fixture()
async def schools(session_factory: Factory) -> AsyncGenerator[list[School]]:
    made: list[School] = []
    yield made
    for school in made:
        await _drop_school(session_factory, school)


async def _school(
    factory: Factory,
    schools: list[School],
    courses: list[str],
    students: dict[str, bool],
) -> School:
    school = await _make_school(factory, courses, students)
    schools.append(school)
    return school


def _order() -> str:
    return f"ord_{uuid.uuid4().hex[:16]}"


async def _buy(
    factory: Factory,
    school: School,
    who: str,
    *,
    units: int,
    paid: int,
    currency: str = "EUR",
    at: datetime,
    tax: int | None = None,
    fee: int | None = None,
    provider: str = CREEM,
    order: str | None = None,
) -> uuid.UUID:
    """A purchase of the student's; ``tax``, ``fee`` — what the provider told
    of it."""
    order = order or _order()
    async with factory() as session:
        credit = await ledger.credit_purchase(
            session,
            account_id=school.accounts[who],
            units=units,
            amount_paid_minor=paid,
            currency=currency,
            provider=provider,
            provider_transaction_id=order,
            provider_line_id=order,
            now=at,
        )
        if tax is not None or fee is not None:
            await provider_figures.record_from_purchase(
                session, batch=credit.batch, tax_minor=tax, fee_minor=fee, now=at
            )
        await session.commit()
        return credit.batch.id


async def _review(
    factory: Factory,
    school: School,
    who: str,
    course: str,
    units: int,
    *,
    at: datetime = IN_OCTOBER,
) -> uuid.UUID:
    """A review of the student's work on the course's task, paid from their
    own account, settled to ``units``."""
    node, task = school.courses[course]
    async with factory() as session:
        submission = HomeworkSubmission(
            tenant_id=school.tenant_id,
            student_id=school.students[who],
            course_node_id=node,
            node_id=node,
            authored_document_id=task,
            file_url="s3://bucket/work.py",
            file_type="text/plain",
            original_filename="work.py",
            status="completed",
        )
        session.add(submission)
        await session.flush()
        await ledger.settle(
            session,
            action_kind=ActionKind.REVIEW,
            action_key=str(submission.id),
            fact_units=units,
            beneficiary_account_id=school.accounts[who],
            payer_source=PayerSource.DEFAULT,
            account_id=school.accounts[who],
            now=at,
        )
        await session.commit()
        return submission.id


async def _calls(
    factory: Factory,
    school: School,
    course: str,
    submission: uuid.UUID,
    calls: list[tuple[str, CostKind, str | None]],
) -> None:
    """The review's job and its calls: ``(action, kind, usd)``."""
    node, _ = school.courses[course]
    at = datetime(2026, 11, 2, tzinfo=UTC)  # another month: dated by the review
    async with factory() as session:
        job = Job(
            tenant_id=school.tenant_id,
            course_node_id=node,
            job_type=JobType.HOMEWORK_PROCESSING.value,
            subject_type=JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING],
            subject_id=submission,
            status="complete",
        )
        session.add(job)
        await session.flush()
        for action, kind, usd in calls:
            exact = kind == CostKind.EXACT
            session.add(
                ExternalServiceCall(
                    job_id=job.id,
                    action=action,
                    provider="gemini",
                    model_id="gemini-2.5-flash",
                    success=True,
                    cost_usd=Decimal(usd) if usd is not None else None,
                    cost_kind=kind.value,
                    catalog_version="test-catalog" if exact else None,
                    tariff_window=TariffWindow.FLAT.value if exact else None,
                    call_started_at=at,
                    created_at=at,
                )
            )
        await session.commit()


async def _figures(
    factory: Factory, batch_id: uuid.UUID
) -> provider_figures.Kept | None:
    """The batch's row of the provider's figures; None — no row."""
    async with factory() as session:
        return (await provider_figures.kept_figures(session, {batch_id})).get(batch_id)


def _three(kept: provider_figures.Kept | None) -> tuple[int | None, ...]:
    assert kept is not None
    figures = kept.figures
    return (figures.tax_minor, figures.fee_minor, figures.refunded_minor)


async def _audit(
    factory: Factory, entity_id: uuid.UUID, action: AuditAction
) -> list[BillingAuditEntry]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(BillingAuditEntry)
                    .where(
                        BillingAuditEntry.entity_id == entity_id,
                        BillingAuditEntry.action == action.value,
                    )
                    .order_by(BillingAuditEntry.created_at, BillingAuditEntry.id)
                )
            ).scalars()
        )


async def _cli(factory: Factory, *argv: str) -> str:
    """One command of ``billing_admin`` in a transaction of its own, as ``run``."""
    args = billing_admin.build_parser().parse_args(list(argv))
    async with factory() as session, session.begin():
        return await billing_admin.COMMANDS[args.command](session, args)


async def _report(factory: Factory, school: School) -> partner_report.PartnerReport:
    """October's report, with the fee tariffs as shipped."""
    async with factory() as session:
        return await partner_report.partner_report(
            session,
            tenant_id=school.tenant_id,
            month=OCTOBER,
            fee_tariffs=SHIPPED_TARIFFS,
        )


def _line(
    figures: partner_report.PartnerFigures, currency: str = "EUR"
) -> partner_report.MoneyLine:
    [line] = [m for m in figures.money if m.currency == currency]
    return line


def _course(
    report: partner_report.PartnerReport, title: str
) -> partner_report.PartnerCourse:
    [found] = [c for c in report.courses if c.title == title]
    return found


# ── Creem's API, replaced ───────────────────────────────────────────────


def _transaction(order: str, **changes: Any) -> dict[str, Any]:
    """A ``TransactionEntity`` as Creem's reference shows it: no fee."""
    return {
        "id": f"tran_{order}",
        "mode": "test",
        "object": "transaction",
        "amount": 1000,
        "amount_paid": 1210,
        "discount_amount": 0,
        "currency": "EUR",
        "type": "payment",
        "tax_country": "NL",
        "tax_amount": 210,
        "status": "paid",
        "refunded_amount": 0,
        "order": order,
        "created_at": 1772445600000,
    } | changes


@dataclass
class CreemApi:
    """Creem's ``GET /transactions/search``: the orders it knows, the orders asked."""

    known: dict[str, dict[str, Any]]
    asked: list[str]

    def answer(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/transactions/search"
        assert request.headers["x-api-key"] == TEST_KEY
        order = request.url.params["order_id"]
        self.asked.append(order)
        items = [self.known[order]] if order in self.known else []
        return httpx.Response(
            200, json={"items": items, "pagination": {"total_records": len(items)}}
        )

    def provider(self) -> CreemProvider:
        return CreemProvider(
            api_key=TEST_KEY,
            webhook_secret=TEST_SECRET,
            transport=httpx.MockTransport(self.answer),
        )


def _signed(payload: dict[str, Any]) -> tuple[dict[str, str], bytes]:
    body = json.dumps(payload).encode()
    signature = hmac.new(TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {SIGNATURE_HEADER: signature}, body


def _checkout_completed(
    event_id: str, account: uuid.UUID, order: str
) -> dict[str, Any]:
    """Creem's sample (https://docs.creem.io/code/webhooks): 1000 + 21 % VAT."""
    return {
        "id": event_id,
        "eventType": "checkout.completed",
        "created_at": 1756684800000,
        "object": {
            "id": f"ch_{event_id}",
            "object": "checkout",
            "order": {
                "object": "order",
                "id": order,
                "amount": 1000,
                "currency": "EUR",
                "sub_total": 1000,
                "tax_amount": 210,
                "amount_due": 1210,
                "amount_paid": 1210,
                "status": "paid",
                "type": "onetime",
                "transaction": f"tran_{order}",
                "mode": "test",
            },
            "units": 1,
            "metadata": {"account_id": str(account), "package_id": "starter"},
            "status": "completed",
            "mode": "test",
        },
    }


# ── The webhook and creem-sync write the figures; once ──────────────


class TestFiguresOfAPurchase:
    async def test_a_a_creem_purchase_brings_its_tax_once(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """``checkout.completed`` carries ``order.tax_amount``: written with
        the credit; the event again changes nothing."""
        school = await _school(session_factory, schools, ["Python"], {"ann": False})
        order, event_id = _order(), f"evt_{uuid.uuid4().hex[:12]}"
        creem = CreemApi({}, []).provider()
        events = creem.parse_webhook(
            *_signed(_checkout_completed(event_id, school.accounts["ann"], order))
        )
        try:
            for at in (SEP_1, SEP_2):
                async with session_factory() as session:
                    [handled] = await handle_events(
                        session,
                        provider=CREEM,
                        events=events,
                        body_sha256=SHA,
                        catalog=CATALOG,
                        test_purchases=False,
                        now=at,
                    )
                assert handled.repeated is (at == SEP_2)
                async with session_factory() as session:
                    batch = (
                        await session.execute(
                            select(BillingBatch).where(
                                BillingBatch.provider_transaction_id == order
                            )
                        )
                    ).scalar_one()
                assert batch.amount_paid_minor == 1210
                kept = await _figures(session_factory, batch.id)
                assert _three(kept) == (210, None, None)
                assert kept is not None and kept.synced_at == SEP_1
        finally:
            async with session_factory() as session:
                await session.execute(
                    delete(BillingProviderEvent).where(
                        BillingProviderEvent.provider_event_id == event_id
                    )
                )
                await session.commit()

    async def test_a_an_event_that_names_the_fee_has_it_written(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """The port carries a fee where a provider gives one (the fake)."""
        school = await _school(session_factory, schools, ["Python"], {"ann": False})
        fake = FakeProvider()
        token = uuid.uuid4().hex[:12]
        headers, body = fake.webhook(
            PURCHASE,
            {
                "custom_data": {
                    "account_id": str(school.accounts["ann"]),
                    "package_id": "starter",
                },
                "transaction_id": f"fake_txn_{token}",
                "line_id": f"fake_line_{token}",
                "amount_minor": 1200,
                "currency": "EUR",
                "tax_minor": 200,
                "fee_minor": 65,
            },
            event_id=f"fake_evt_{token}",
        )
        try:
            async with session_factory() as session:
                await handle_events(
                    session,
                    provider=fake.name,
                    events=fake.parse_webhook(headers, body),
                    body_sha256=SHA,
                    catalog=CATALOG,
                    test_purchases=False,
                    now=SEP_1,
                )
            async with session_factory() as session:
                batch = (
                    await session.execute(
                        select(BillingBatch).where(
                            BillingBatch.provider_transaction_id == f"fake_txn_{token}"
                        )
                    )
                ).scalar_one()
            assert _three(await _figures(session_factory, batch.id)) == (200, 65, None)
        finally:
            async with session_factory() as session:
                await session.execute(
                    delete(BillingProviderEvent).where(
                        BillingProviderEvent.provider_event_id == f"fake_evt_{token}"
                    )
                )
                await session.commit()

    async def test_a_creem_sync_writes_the_tax_and_the_refunded_part_once(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """``creem-sync``: the period's paid batches asked by their order;
        a dry run counts and writes nothing; the run writes and audits; a
        second run changes nothing. No fee: Creem's API has none."""
        school = await _school(session_factory, schools, ["Python"], {"ann": False})
        order, before = _order(), _order()
        batch_id = await _buy(
            session_factory,
            school,
            "ann",
            units=100,
            paid=1210,
            at=SYNC_DAY,
            order=order,
        )
        await _buy(
            session_factory,
            school,
            "ann",
            units=100,
            paid=1210,
            at=datetime(2026, 3, 1, 23, 59, tzinfo=UTC),
            order=before,
        )
        api = CreemApi(
            {order: _transaction(order, status="partialRefund", refunded_amount=400)},
            [],
        )
        sync = ["creem-sync", "--from", "2026-03-02", "--to", "2026-03-02"]
        with patch.object(billing_admin, "creem_lookup", api.provider):
            dry = await _cli(session_factory, *sync, "--actor", "k", "--dry-run")
            assert "would update=1" in dry and "dry run: nothing written" in dry
            assert await _figures(session_factory, batch_id) is None

            out = await _cli(session_factory, *sync, "--actor", "kostiantyn")
            assert "updated=1  unchanged=0" in out
            assert "does not give it" in out
            synced = await _figures(session_factory, batch_id)
            assert _three(synced) == (210, None, 400)

            again = await _cli(session_factory, *sync, "--actor", "kostiantyn")
            assert "updated=0  unchanged=1" in again
            assert await _figures(session_factory, batch_id) == synced
        assert api.asked == [order, order, order]  # the other day's batch: never

        [entry] = await _audit(
            session_factory, batch_id, AuditAction.BATCH_PROVIDER_SYNC
        )
        assert entry.actor == "kostiantyn" and entry.entity == "batch"
        assert entry.before is not None and entry.after is not None
        assert entry.before == {
            "tax_minor": None,
            "fee_minor": None,
            "refunded_minor": None,
            "synced_at": None,
        }
        assert entry.after["tax_minor"] == 210
        assert entry.after["refunded_minor"] == 400
        assert entry.after["fee_minor"] is None
        assert synced is not None
        assert entry.after["synced_at"] == synced.synced_at.isoformat()

    async def test_a_the_fee_is_synced_where_the_provider_gives_it(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """The sync writes the fee where the provider gives it, and nothing
        in another currency than the batch's."""
        school = await _school(session_factory, schools, ["Python"], {"ann": False})
        euro, dollar = f"fake_txn_{uuid.uuid4().hex}", f"fake_txn_{uuid.uuid4().hex}"
        euro_batch = await _buy(
            session_factory,
            school,
            "ann",
            units=100,
            paid=1200,
            at=SYNC_DAY,
            provider="fake",
            order=euro,
        )
        dollar_batch = await _buy(
            session_factory,
            school,
            "ann",
            units=100,
            paid=1200,
            at=SYNC_DAY,
            provider="fake",
            order=dollar,
        )
        fake = FakeProvider()
        fake.transactions[euro] = TransactionFigures(
            currency="EUR", tax_minor=200, fee_minor=61, refunded_minor=0
        )
        fake.transactions[dollar] = TransactionFigures(
            currency="USD", tax_minor=200, fee_minor=61, refunded_minor=0
        )
        async with session_factory() as session:
            done = await provider_figures.sync_batches(
                session,
                fake,
                from_date=SYNC_DAY.date(),
                to_date=SYNC_DAY.date(),
                actor="kostiantyn",
                dry_run=False,
                now=SYNC_DAY,
            )
            await session.commit()
        assert (done.batches, done.updated, done.other_currency) == (2, 1, 1)
        assert _three(await _figures(session_factory, euro_batch)) == (200, 61, 0)
        assert await _figures(session_factory, dollar_batch) is None


# ── The batch stays insert-only; the figures are upserted beside it ──


class TestBatchStaysInsertOnly:
    async def test_ye_an_update_of_any_column_of_the_batch_is_refused(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """Every column of ``billing_batches``, the amount among them, even
        set to itself: the trigger refuses, as in main; the batch is as it was."""
        school = await _school(session_factory, schools, ["Python"], {"ann": False})
        batch_id = await _buy(
            session_factory, school, "ann", units=100, paid=1210, tax=210, at=SEP_1
        )
        async with session_factory() as session:
            before = (
                await session.execute(
                    text("SELECT to_jsonb(b) FROM billing_batches b WHERE id = :b"),
                    {"b": batch_id},
                )
            ).scalar_one()
            columns = sorted(before)
            assert "amount_paid_minor" in columns
            assert not {c for c in columns if c.startswith("provider_")} - {
                "provider",
                "provider_transaction_id",
                "provider_line_id",
            }  # no column of the provider's figures on the batch
            for refused in [
                "amount_paid_minor = amount_paid_minor + 1",
                *(f"{column} = {column}" for column in columns),
            ]:
                with pytest.raises(DBAPIError, match="insert-only: UPDATE refused"):
                    async with session.begin_nested():
                        await session.execute(
                            text(f"UPDATE billing_batches SET {refused} WHERE id = :b"),  # noqa: S608 — names from the table itself
                            {"b": batch_id},
                        )
            after = (
                await session.execute(
                    text("SELECT to_jsonb(b) FROM billing_batches b WHERE id = :b"),
                    {"b": batch_id},
                )
            ).scalar_one()
        assert after == before

    async def test_ye_the_figures_upsert_is_allowed_and_idempotent(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """The figures' row: inserted, merged (a None keeps the kept figure),
        the same figures again write nothing (``synced_at`` stays); the batch
        never changes."""
        school = await _school(session_factory, schools, ["Python"], {"ann": False})
        batch_id = await _buy(
            session_factory, school, "ann", units=100, paid=1210, at=SEP_1
        )

        async def record(tax: int | None, fee: int | None, at: datetime) -> bool:
            async with session_factory() as session:
                batch = await session.get(BillingBatch, batch_id)
                assert batch is not None
                done = await provider_figures.record_from_purchase(
                    session, batch=batch, tax_minor=tax, fee_minor=fee, now=at
                )
                await session.commit()
                return done

        assert await _figures(session_factory, batch_id) is None
        assert await record(None, None, SEP_1) is False  # nothing given: no row
        assert await _figures(session_factory, batch_id) is None

        assert await record(210, None, SEP_1) is True
        first = await _figures(session_factory, batch_id)
        assert _three(first) == (210, None, None)
        assert first is not None and first.synced_at == SEP_1

        for _ in range(2):  # the same again, or only a None: nothing written
            assert await record(210, None, SEP_2) is False
            assert await record(None, None, SEP_2) is False
        assert await _figures(session_factory, batch_id) == first

        assert await record(None, 61, SEP_3) is True  # a None keeps the tax
        merged = await _figures(session_factory, batch_id)
        assert _three(merged) == (210, 61, None)
        assert merged is not None and merged.synced_at == SEP_3

        async with session_factory() as session:  # the upsert itself, in SQL
            for _ in range(2):
                await session.execute(
                    text(
                        "INSERT INTO billing_batch_provider_figures "
                        "(batch_id, tax_minor, fee_minor, refunded_minor, synced_at) "
                        "VALUES (:b, 300, NULL, 0, now()) "
                        "ON CONFLICT (batch_id) DO UPDATE SET "
                        "tax_minor = EXCLUDED.tax_minor, "
                        "refunded_minor = EXCLUDED.refunded_minor, "
                        "synced_at = EXCLUDED.synced_at"
                    ),
                    {"b": batch_id},
                )
            await session.commit()
        assert _three(await _figures(session_factory, batch_id)) == (300, 61, 0)
        async with session_factory() as session:
            rows = await session.scalar(
                select(func.count())
                .select_from(BillingBatchProviderFigures)
                .where(BillingBatchProviderFigures.batch_id == batch_id)
            )
            batch = await session.get(BillingBatch, batch_id)
        assert rows == 1
        assert batch is not None and batch.amount_paid_minor == 1210

    async def test_ye_a_figure_is_never_negative(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        school = await _school(session_factory, schools, ["Python"], {"ann": False})
        batch_id = await _buy(
            session_factory, school, "ann", units=100, paid=1210, at=SEP_1
        )
        async with session_factory() as session:
            for column in ("tax_minor", "fee_minor", "refunded_minor"):
                with pytest.raises(DBAPIError, match="not_negative"):
                    async with session.begin_nested():
                        await session.execute(
                            text(
                                "INSERT INTO billing_batch_provider_figures "  # noqa: S608 — names from the test
                                f"(batch_id, {column}, synced_at) "
                                "VALUES (:b, -1, now())"
                            ),
                            {"b": batch_id},
                        )


# ── The tax in proportion; Creem gives no fee ───────────────────


async def _two_batches_world(factory: Factory, schools: list[School]) -> School:
    """Ann's two batches of different price and tax, Bob's batch Creem has not
    told the tax of; four reviews in October (module docstring of the test)."""
    school = await _school(
        factory, schools, ["Data", "Python"], {"ann": False, "bob": False}
    )
    # A: 100 units for 1210 with 210 of tax; B: 200 units for 3000 with 500.
    await _buy(factory, school, "ann", units=100, paid=1210, tax=210, at=SEP_1)
    await _buy(factory, school, "ann", units=200, paid=3000, tax=500, at=SEP_2)
    # C: 40 units for 500, no data from Creem yet.
    await _buy(factory, school, "bob", units=40, paid=500, at=SEP_3)
    first = await _review(factory, school, "ann", "Python", 60)  # A 60
    await _review(factory, school, "ann", "Python", 60)  # A 40 + B 20
    await _review(factory, school, "ann", "Data", 50)  # B 50
    await _review(factory, school, "bob", "Data", 20)  # C 20
    await _calls(
        factory,
        school,
        "Python",
        first,
        [
            ("review", CostKind.EXACT, "0.250000"),
            ("review", CostKind.ESTIMATED, "0.100000"),
            ("review", CostKind.UNKNOWN, None),
            ("criteria_decomposition", CostKind.EXACT, "5.000000"),  # a preparation
        ],
    )
    return school


class TestTaxInProportion:
    async def test_v_the_tax_is_the_units_spent_of_each_batch(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """gross: A 1210 + B 70/200 x 3000 + C 20/40 x 500 = 2510; tax:
        A 210 + B 70/200 x 500 = 385 — not the batches' whole tax (710); C adds
        none and is counted."""
        school = await _two_batches_world(session_factory, schools)
        report = await _report(session_factory, school)

        total = _line(report.totals)
        assert (total.gross_minor, total.tax_minor, total.net_minor) == (
            2510,
            385,
            2125,
        )
        assert total.without_provider_data == (("creem", 1),)
        assert report.totals.reviews == 4

        python = _line(_course(report, "Python").figures)
        assert (python.gross_minor, python.tax_minor, python.net_minor) == (
            1510,  # A 100 units + B 20 units
            260,  # A 210 + B 20/200 x 500
            1250,
        )
        assert python.without_provider_data == ()
        data = _line(_course(report, "Data").figures)
        assert (data.gross_minor, data.tax_minor, data.net_minor) == (1000, 125, 875)
        assert data.without_provider_data == (("creem", 1),)
        [task] = _course(report, "Data").tasks
        assert task.title == "Завдання Data"
        assert _line(task.figures).net_minor == 875

    async def test_the_model_cost_is_of_these_reviews(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """The review's paid calls, a preparation stage left out, whenever made."""
        school = await _two_batches_world(session_factory, schools)
        report = await _report(session_factory, school)
        python = _course(report, "Python").figures
        assert python.model_cost_usd == Decimal("0.35")
        assert python.unknown_cost_calls == 1
        assert _course(report, "Data").figures.model_cost_usd == Decimal("0")
        assert report.totals.model_cost_usd == Decimal("0.35")

    async def test_b_creem_gives_no_fee_and_the_report_says_so(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """Creem's API gives no fee (the figures' ``fee_minor`` is empty) and
        its tariff has no fixed part in EUR: ``fee_minor`` None, net = gross -
        tax, the line names Creem; the text says «комісія Creem: тариф для EUR
        не задано»."""
        school = await _two_batches_world(session_factory, schools)
        report = await _report(session_factory, school)
        total = _line(report.totals)
        assert total.fee_minor is None
        assert total.fee_unknown == ("creem",)
        assert total.fee_by_tariff == ()
        assert total.net_minor == total.gross_minor - total.tax_minor

        out = await _cli(
            session_factory,
            "partner-report",
            "--tenant",
            school.name,
            "--month",
            "2026-10",
        )
        assert "комісія Creem: тариф для EUR не задано" in out
        assert "розрахунково за тарифом" not in out
        assert "без даних Creem: 1 парт." in out
        assert "fee_minor=-" in out


# ── Creem's fee by its tariff (Б-5) ──────────────────────────────────


class TestCreemFeeByTariff:
    async def test_zh_the_fee_is_the_tariffs_where_set_the_apis_first(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """Creem as shipped: 3.9 % of the amount paid (tax included), rounded
        half up, + USD 40 once per batch; EUR not set.

        Ann's two USD batches by the tariff — A: 1500 x 3.9 % = 58.5 → 59
        + 40 = 99; B: 2000 x 3.9 % = 78 + 40 = 118. Python spends A 100 of 100
        and B 50 of 200: fee 99 + 118/4 = 128.5 → 129. Bob's EUR batch: no
        fee, «тариф для EUR не задано». Cat's USD batch has a fee from the
        API (77; the tariff would give 79): Rust spends half — 38.5 → 39, the
        API's, not the tariff's."""
        school = await _school(
            session_factory,
            schools,
            ["Data", "Python", "Rust"],
            {"ann": False, "bob": False, "cat": False},
        )
        for who, units, paid, tax, fee, at in (
            ("ann", 100, 1500, 250, None, SEP_1),  # A
            ("ann", 200, 2000, 300, None, SEP_2),  # B
            ("cat", 100, 1000, 100, 77, SEP_1),  # D: the API's fee
        ):
            await _buy(
                session_factory,
                school,
                who,
                units=units,
                paid=paid,
                tax=tax,
                fee=fee,
                currency="USD",
                at=at,
            )
        await _buy(  # C, in EUR
            session_factory, school, "bob", units=100, paid=1000, tax=200, at=SEP_1
        )
        await _review(session_factory, school, "ann", "Python", 100)  # A 100
        await _review(session_factory, school, "ann", "Python", 50)  # B 50
        await _review(session_factory, school, "bob", "Data", 40)  # C 40
        await _review(session_factory, school, "cat", "Rust", 50)  # D 50

        creem = SHIPPED_TARIFFS[CREEM]
        assert creem.percent == Decimal("3.9")
        assert creem.fixed_minor == {"USD": 40, "EUR": None}

        report = await _report(session_factory, school)

        python = _line(_course(report, "Python").figures, "USD")
        assert (
            python.gross_minor,
            python.tax_minor,
            python.fee_minor,
            python.net_minor,
        ) == (2000, 325, 129, 1546)
        assert python.fee_by_tariff == ("creem",)
        assert python.fee_unknown == ()

        data = _line(_course(report, "Data").figures, "EUR")
        assert (data.gross_minor, data.tax_minor, data.fee_minor, data.net_minor) == (
            400,
            80,
            None,
            320,
        )
        assert data.fee_unknown == ("creem",)
        assert data.fee_by_tariff == ()

        rust = _line(_course(report, "Rust").figures, "USD")
        assert (rust.gross_minor, rust.tax_minor, rust.fee_minor, rust.net_minor) == (
            500,
            50,
            39,
            411,
        )
        assert rust.fee_by_tariff == ()  # the API's fee has precedence
        assert rust.fee_unknown == ()

        dollars = _line(report.totals, "USD")
        assert (dollars.gross_minor, dollars.tax_minor, dollars.fee_minor) == (
            2500,
            375,
            167,  # 128.5 + 38.5, rounded once
        )
        assert dollars.net_minor == 2500 - 375 - 167
        assert dollars.fee_by_tariff == ("creem",)
        assert _line(report.totals, "EUR").fee_unknown == ("creem",)

        get_catalog(Path("config/billing_packages.yaml"))  # the CLI's: as shipped
        out = await _cli(
            session_factory,
            "partner-report",
            "--tenant",
            school.name,
            "--month",
            "2026-10",
        )
        assert "комісія Creem — розрахунково за тарифом" in out
        assert "комісія Creem: тариф для EUR не задано" in out
        rows = list(
            csv.DictReader(
                io.StringIO(
                    await _cli(
                        session_factory,
                        "partner-report",
                        "--tenant",
                        school.name,
                        "--month",
                        "2026-10",
                        "--format",
                        "csv",
                    )
                )
            )
        )
        [rust_row] = [
            r for r in rows if r["level"] == "course" and r["course_title"] == "Rust"
        ]
        assert (rust_row["fee_minor"], rust_row["notes"]) == ("39", "")

    async def test_zh_a_report_without_tariffs_counts_no_fee(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """No tariff given: a USD batch has no fee either — the tariff is
        what counts it, nothing else."""
        school = await _school(session_factory, schools, ["Python"], {"ann": False})
        await _buy(
            session_factory,
            school,
            "ann",
            units=100,
            paid=1500,
            tax=250,
            currency="USD",
            at=SEP_1,
        )
        await _review(session_factory, school, "ann", "Python", 100)
        async with session_factory() as session:
            report = await partner_report.partner_report(
                session, tenant_id=school.tenant_id, month=OCTOBER, fee_tariffs={}
            )
        line = _line(report.totals, "USD")
        assert (line.fee_minor, line.fee_unknown, line.fee_by_tariff) == (
            None,
            ("creem",),
            (),
        )
        assert line.net_minor == 1500 - 250


# ── The rate ────────────────────────────────────────────────────────


class TestRate:
    async def test_g_the_rate_is_set_changed_and_removed_audited(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """Each change audited as ``author_settings.update``; the same rate
        again writes nothing."""
        school = await _school(session_factory, schools, ["Python"], {})
        share = ["set-partner-share", "--tenant", str(school.tenant_id)]

        async def rate() -> int | None:
            async with session_factory() as session:
                row = await session.get(BillingAuthorSettings, school.author_account)
                return row.partner_share_bps if row is not None else None

        async def audited() -> list[BillingAuditEntry]:
            return await _audit(
                session_factory,
                school.author_account,
                AuditAction.AUTHOR_SETTINGS_UPDATE,
            )

        out = await _cli(session_factory, *share, "--percent", "12.5", "--actor", "k")
        assert "partner_share=12.5 %" in out
        assert await rate() == 1250
        assert len(await audited()) == 1

        await _cli(session_factory, *share, "--percent", "12.50", "--actor", "k")
        assert len(await audited()) == 1  # the same rate: nothing written

        await _cli(session_factory, *share, "--percent", "7.25", "--actor", "k")
        assert await rate() == 725
        await _cli(session_factory, *share, "--percent", "none", "--actor", "k")
        assert await rate() is None
        entries = await audited()
        assert [(e.before or {}).get("partner_share_bps") for e in entries] == [
            None,
            1250,
            725,
        ]
        assert [(e.after or {}).get("partner_share_bps") for e in entries] == [
            1250,
            725,
            None,
        ]

    @pytest.mark.parametrize("percent", ["100.01", "150", "-1", "-0.5"])
    async def test_g_a_rate_out_of_range_is_refused(
        self, session_factory: Factory, schools: list[School], percent: str
    ) -> None:
        """Over 100 or below 0: refused, nothing written."""
        school = await _school(session_factory, schools, ["Python"], {})
        with pytest.raises(BillingInvalidError, match="0 to 100"):
            await _cli(
                session_factory,
                "set-partner-share",
                "--tenant",
                str(school.tenant_id),
                "--percent",
                percent,
                "--actor",
                "k",
            )
        assert not await _audit(
            session_factory, school.author_account, AuditAction.AUTHOR_SETTINGS_UPDATE
        )

    @pytest.mark.parametrize(
        "percent", ["12.345", "abc", "nan", "sNaN", "Infinity", "", "1e-3"]
    )
    def test_g_a_rate_that_is_not_a_percent_is_a_usage_error(
        self, percent: str
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            billing_admin.build_parser().parse_args(
                [
                    "set-partner-share",
                    "--tenant",
                    "t",
                    "--percent",
                    percent,
                    "--actor",
                    "k",
                ]
            )
        assert exc.value.code == 2

    async def test_g_the_share_is_the_rate_times_the_net(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """12.5 % of each line's net, rounded half up; without a rate, no sum."""
        school = await _two_batches_world(session_factory, schools)
        without = await _report(session_factory, school)
        assert without.share_bps is None
        assert _line(without.totals).partner_share_minor is None
        text_out = await _cli(
            session_factory,
            "partner-report",
            "--tenant",
            school.name,
            "--month",
            "2026-10",
        )
        assert "share rate: ставку не задано" in text_out
        assert "partner_share_minor=ставку не задано" in text_out

        await _cli(
            session_factory,
            "set-partner-share",
            "--tenant",
            school.name,
            "--percent",
            "12.5",
            "--actor",
            "k",
        )
        report = await _report(session_factory, school)
        assert report.share_bps == 1250
        assert _line(report.totals).partner_share_minor == 266  # 2125 x 0.125 = 265.625
        assert _line(_course(report, "Python").figures).partner_share_minor == 156
        assert _line(_course(report, "Data").figures).partner_share_minor == 109


# ── Who is in ───────────────────────────────────────────────────────


class TestWhoIsIn:
    async def test_gg_service_students_and_other_schools_are_out_currencies_apart(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """A service student's review is counted apart; another school's
        money and calls are not here; EUR and USD are two lines."""
        school = await _school(
            session_factory,
            schools,
            ["Python"],
            {"ann": False, "cat": False, "svc": True},
        )
        other = await _school(session_factory, schools, ["Python"], {"dan": False})
        await _buy(
            session_factory, school, "ann", units=100, paid=1000, tax=200, at=SEP_1
        )
        await _buy(
            session_factory,
            school,
            "cat",
            units=100,
            paid=1100,
            tax=100,
            currency="USD",
            at=SEP_1,
        )
        await _buy(
            session_factory, school, "svc", units=100, paid=1000, tax=200, at=SEP_1
        )
        await _buy(
            session_factory, other, "dan", units=100, paid=9000, tax=900, at=SEP_1
        )
        await _review(session_factory, school, "ann", "Python", 10)
        await _review(session_factory, school, "cat", "Python", 30)
        await _review(session_factory, school, "svc", "Python", 50)
        theirs = await _review(session_factory, other, "dan", "Python", 50)
        await _calls(
            session_factory,
            other,
            "Python",
            theirs,
            [("review", CostKind.EXACT, "9.0")],
        )

        report = await _report(session_factory, school)
        assert report.totals.reviews == 2
        assert report.excluded_test_reviews == 1
        assert report.totals.model_cost_usd == Decimal("0")
        euro, dollar = report.totals.money
        assert (euro.currency, euro.gross_minor, euro.tax_minor) == ("EUR", 100, 20)
        assert (dollar.currency, dollar.gross_minor, dollar.tax_minor) == (
            "USD",
            330,
            30,
        )
        [course] = report.courses
        assert [m.currency for m in course.figures.money] == ["EUR", "USD"]

        rows = list(
            csv.DictReader(
                io.StringIO(
                    await _cli(
                        session_factory,
                        "partner-report",
                        "--tenant",
                        str(school.tenant_id),
                        "--month",
                        "2026-10",
                        "--format",
                        "csv",
                    )
                )
            )
        )
        totals = [r for r in rows if r["level"] == "total"]
        assert [(r["currency"], r["gross_minor"], r["reviews"]) for r in totals] == [
            ("EUR", "100", "2"),
            ("USD", "330", ""),  # the row's reviews on its first line only
        ]

        theirs_report = await _report(session_factory, other)
        assert _line(theirs_report.totals).gross_minor == 4500
        assert theirs_report.totals.model_cost_usd == Decimal("9.0")


# ── Refunds and the month's edges ───────────────────────────────────


class TestMonth:
    async def test_d_the_edges_are_utc_and_inclusive_refunds_apart(
        self, session_factory: Factory, schools: list[School]
    ) -> None:
        """Debits on the first instant and the last second of October are
        in, the instants around them out; refunds decided in October are a
        line apart and subtract nothing."""
        school = await _school(
            session_factory, schools, ["Python"], {"ann": False, "svc": True}
        )
        order, service_order = _order(), _order()
        await _buy(
            session_factory,
            school,
            "ann",
            units=100,
            paid=1000,
            tax=0,
            at=SEP_1,
            order=order,
        )
        await _buy(
            session_factory,
            school,
            "svc",
            units=100,
            paid=1000,
            at=SEP_1,
            order=service_order,
        )
        for at in (
            datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 10, 1, tzinfo=UTC),
            datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 11, 1, tzinfo=UTC),
        ):
            await _review(session_factory, school, "ann", "Python", 10, at=at)
        for adjustment, account_order, amount, at in (
            ("ref_in", order, 300, datetime(2026, 10, 20, tzinfo=UTC)),
            ("ref_out", order, 100, datetime(2026, 11, 1, tzinfo=UTC)),
            ("ref_svc", service_order, 500, datetime(2026, 10, 20, tzinfo=UTC)),
        ):
            async with session_factory() as session:
                await refund_at_provider(
                    session,
                    provider=CREEM,
                    provider_adjustment_id=f"{adjustment}_{uuid.uuid4().hex[:8]}",
                    transaction_id=account_order,
                    amount_minor=amount,
                    currency="EUR",
                    now=at,
                )
                await session.commit()

        report = await _report(session_factory, school)
        assert (report.from_date, report.to_date) == (
            date(2026, 10, 1),
            date(2026, 10, 31),
        )
        assert report.totals.reviews == 2
        assert _line(report.totals).gross_minor == 200  # refunds subtract nothing
        assert report.refunds == (
            partner_report.RefundLine(
                kind="refund", currency="EUR", count=1, units=30, amount_minor=300
            ),
        )
        as_json = json.loads(
            await _cli(
                session_factory,
                "partner-report",
                "--tenant",
                school.name,
                "--month",
                "2026-10",
                "--format",
                "json",
            )
        )
        assert as_json["internal"] is True
        assert as_json["refunds"] == [
            {
                "kind": "refund",
                "currency": "EUR",
                "count": 1,
                "units": 30,
                "amount_minor": 300,
            }
        ]
        assert as_json["totals"]["money"][0]["net_minor"] == 200


# ── The migration ───────────────────────────────────────────────────


def test_e_the_migration_goes_down_and_up_on_one_head() -> None:
    """One head, over ``b4_promotion_funnel``. Down to it: the figures' table
    and the rate gone; up again: back. ``billing_batches`` is the same at both
    ends — its columns, and its trigger on the shared function."""
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["b3_partner_report"]
    revision = script.get_revision("b3_partner_report")
    assert revision is not None and revision.down_revision == "b4_promotion_funnel"
    engine = create_engine(get_settings().database_url)
    function = text(
        "SELECT p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
        "WHERE t.tgname = 'billing_batches_insert_only'"
    )

    def tables() -> set[str]:
        with engine.connect() as conn:
            return set(inspect(conn).get_table_names())

    def columns(table: str) -> set[str]:
        with engine.connect() as conn:
            return {c["name"] for c in inspect(conn).get_columns(table)}

    def trigger() -> str | None:
        with engine.connect() as conn:
            found = conn.scalar(function)
        return None if found is None else str(found)

    figures = "billing_batch_provider_figures"
    try:
        batches = columns("billing_batches")
        assert figures in tables()
        assert columns(figures) == {
            "batch_id",
            "tax_minor",
            "fee_minor",
            "refunded_minor",
            "synced_at",
        }
        assert trigger() == "billing_refuse_update"

        command.downgrade(cfg, "b4_promotion_funnel")
        assert figures not in tables()
        assert "partner_share_bps" not in columns("billing_author_settings")
        assert columns("billing_batches") == batches
        assert trigger() == "billing_refuse_update"

        command.upgrade(cfg, "head")
        assert figures in tables()
        assert "partner_share_bps" in columns("billing_author_settings")
        assert columns("billing_batches") == batches
        assert trigger() == "billing_refuse_update"
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()
