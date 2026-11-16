"""BIL-TESTMARK on a live database: test purchases marked, their units voided.

* The mark: Creem's ``checkout.completed`` through the webhook route — the
  adapter in its test mode marks the batch in the credit's transaction; in
  live mode nothing is marked; a failure after the credit leaves neither.
* ``void-test-batches`` (``scripts/billing_admin.py``, run as an operator
  runs it: its own engine, its own committed transaction): only the free
  units of marked batches go; reserved and held units stay and are printed;
  a repeat changes nothing; ``--dry-run`` writes nothing; the balance, the
  statement, the audit entry, ``show-account``.
* The table: insert-only. The migration: its backfill marks Creem purchases
  only; down and up; the downgrade refused while marks or voids exist.

Every test commits and removes what it wrote: the migration tests of other
modules walk the schema down, which this revision refuses while a mark or a
``test_voided`` movement exists.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient, Response
from scripts import billing_admin
from sqlalchemy import create_engine, delete, inspect, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_billing_catalog,
    get_payment_registry,
    get_session,
)
from course_supporter.billing import ledger, refunds, testmark
from course_supporter.billing.kinds import (
    ActionKind,
    AuditAction,
    MarkOrigin,
    MovementKind,
)
from course_supporter.billing.payments.catalog import Catalog, Package
from course_supporter.billing.payments.creem import (
    CREEM,
    LIVE_API_BASE,
    TEST_API_BASE,
    CreemProvider,
)
from course_supporter.billing.payments.registry import ProviderRegistry
from course_supporter.billing.statement import StatementLineKind, statement_page
from course_supporter.config import get_settings
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

ACTOR = "test-admin"
BELOW = "bile_charging_order_hold"
WEBHOOK = f"/api/v1/billing/webhooks/{CREEM}"
CATALOG = Catalog(
    packages=(
        Package(id="starter", units=1000, prices_minor={"EUR": 990}, active=True),
    ),
    providers={CREEM: {"starter": "prod_starter"}},
)


@dataclass
class World:
    tenant_id: uuid.UUID
    student_account: uuid.UUID
    author_account: uuid.UUID
    providers: list[CreemProvider] = field(default_factory=list)


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[World]:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-testmark-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        student = Student(tenant_id=tenant.id, external_id="testmark-student")
        session.add(student)
        await session.flush()
        student_account = BillingAccount(
            kind="student", tenant_id=tenant.id, student_id=student.id
        )
        author_account = BillingAccount(kind="author", tenant_id=tenant.id)
        session.add_all([student_account, author_account])
        await session.commit()
    made = World(tenant.id, student_account.id, author_account.id)

    yield made

    async with session_factory() as session:
        accounts = [made.student_account, made.author_account]
        batches = list(
            (
                await session.execute(
                    select(BillingBatch.id).where(BillingBatch.account_id.in_(accounts))
                )
            ).scalars()
        )
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


async def _marks(
    session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> list[tuple[BillingBatch, BillingTestBatch | None]]:
    """The account's batches, each with its mark (or None)."""
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingBatch, BillingTestBatch)
            .outerjoin(BillingTestBatch, BillingTestBatch.batch_id == BillingBatch.id)
            .where(BillingBatch.account_id == account_id)
            .order_by(BillingBatch.created_at)
        )
        return [(batch, mark) for batch, mark in rows.tuples()]


# ── The mark, through the webhook ──────────────────────────────────────


def _creem(api_base: str) -> CreemProvider:
    return CreemProvider(
        api_key="creem_key_for_tests_only",
        webhook_secret="whsec_for_tests_only",
        api_base=api_base,
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )


@pytest.fixture()
def routes(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> Iterator[None]:
    async def _session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_payment_registry] = lambda: ProviderRegistry(
        world.providers
    )
    app.dependency_overrides[get_billing_catalog] = lambda: CATALOG
    yield
    app.dependency_overrides.clear()


async def _paid(creem: CreemProvider, account_id: uuid.UUID, order: str) -> Response:
    """Creem's signed ``checkout.completed`` of the starter package."""
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
                    "amount_paid": 990,
                    "currency": "EUR",
                    "status": "paid",
                },
                "units": 1,
                "metadata": {"account_id": str(account_id), "package_id": "starter"},
            },
        }
    ).encode()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            WEBHOOK, content=body, headers={"creem-signature": creem.sign(body)}
        )


def _order() -> str:
    return f"ord_{uuid.uuid4().hex[:16]}"


@pytest.mark.usefixtures("routes")
class TestMarkOnCredit:
    async def test_a_purchase_in_test_mode_gets_its_batch_and_its_mark_together(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        creem = _creem(TEST_API_BASE)
        assert creem.test_mode
        world.providers.append(creem)

        response = await _paid(creem, world.student_account, _order())

        assert response.status_code == 200, response.text
        [(batch, mark)] = await _marks(session_factory, world.student_account)
        assert (batch.provider, batch.units) == (CREEM, 1000)
        assert mark is not None
        assert mark.marked_by == MarkOrigin.CREDIT.value
        assert mark.marked_at == batch.created_at  # one transaction, one instant

    async def test_a_purchase_in_live_mode_is_not_marked(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        creem = _creem(LIVE_API_BASE)
        assert not creem.test_mode
        world.providers.append(creem)

        response = await _paid(creem, world.student_account, _order())

        assert response.status_code == 200, response.text
        [(batch, mark)] = await _marks(session_factory, world.student_account)
        assert (batch.provider, mark) == (CREEM, None)

    async def test_a_failure_after_the_credit_leaves_neither_batch_nor_mark(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        creem = _creem(TEST_API_BASE)
        world.providers.append(creem)
        order = _order()

        with patch(
            "course_supporter.billing.payments.webhooks.link_purchase",
            AsyncMock(side_effect=RuntimeError("after the credit and the mark")),
        ):
            response = await _paid(creem, world.student_account, order)

        assert response.status_code == 500
        assert await _marks(session_factory, world.student_account) == []
        async with session_factory() as session:
            assert await session.scalar(select(BillingTestBatch.batch_id)) is None
            event = (
                await session.execute(
                    select(BillingProviderEvent).where(
                        BillingProviderEvent.provider_event_id == f"evt_{order}"
                    )
                )
            ).scalar_one()
        assert (event.state, event.error) == ("failed", "RuntimeError")

    async def test_a_repeated_purchase_is_marked_once(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        creem = _creem(TEST_API_BASE)
        world.providers.append(creem)
        order = _order()

        first = await _paid(creem, world.student_account, order)
        again = await _paid(creem, world.student_account, order)

        assert (first.status_code, again.status_code) == (200, 200)
        [(_, mark)] = await _marks(session_factory, world.student_account)
        assert mark is not None


# ── void-test-batches ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Scene:
    """A student account: marked batch ``marked`` (1000 units, 100 of them
    reserved, 50 held for a refund) and an administrator's credit
    ``unmarked`` (200); an author account: marked batch ``author_marked``
    (500, all free)."""

    marked: uuid.UUID
    unmarked: uuid.UUID
    author_marked: uuid.UUID
    action_key: str


@pytest.fixture()
async def scene(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> Scene:
    now = datetime.now(UTC)
    action_key = f"review-{uuid.uuid4().hex[:12]}"
    async with session_factory() as session:
        marked = await ledger.credit_purchase(
            session,
            account_id=world.student_account,
            units=1000,
            amount_paid_minor=990,
            currency="EUR",
            provider=CREEM,
            provider_transaction_id=_order(),
            provider_line_id=_order(),
            now=now - timedelta(days=2),  # expires first: the reserve takes it
        )
        unmarked = await ledger.credit_admin(
            session,
            account_id=world.student_account,
            units=200,
            reason="a pilot",
            actor=ACTOR,
            now=now - timedelta(days=1),
        )
        author = await ledger.credit_purchase(
            session,
            account_id=world.author_account,
            units=500,
            amount_paid_minor=500,
            currency="EUR",
            provider=CREEM,
            provider_transaction_id=_order(),
            provider_line_id=_order(),
            now=now - timedelta(days=2),
        )
        for credit in (marked, author):
            await testmark.mark(
                session,
                batch_id=credit.batch.id,
                origin=MarkOrigin.CREDIT,
                now=credit.batch.created_at,
            )
        await ledger.reserve(
            session,
            action_kind=ActionKind.REVIEW,
            action_key=action_key,
            account_id=world.student_account,
            units=100,
        )
        await refunds.refund_hold(
            session,
            account_id=world.student_account,
            batch_id=marked.batch.id,
            units=50,
            provider=CREEM,
            provider_adjustment_id=f"ref_{uuid.uuid4().hex[:12]}",
        )
        await session.commit()
    return Scene(marked.batch.id, unmarked.batch.id, author.batch.id, action_key)


async def _admin(*argv: str) -> int:
    """The script as an operator runs it: its own event loop and engine."""
    return await asyncio.to_thread(billing_admin.main, list(argv))


async def _void(*extra: str) -> int:
    return await _admin("void-test-batches", "--actor", ACTOR, *extra)


async def _voided(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> dict[uuid.UUID, int]:
    """Units voided per batch, on the world's accounts."""
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingMovement.batch_id, BillingMovement.units).where(
                BillingMovement.kind == MovementKind.TEST_VOIDED.value,
                BillingMovement.account_id.in_(
                    [world.student_account, world.author_account]
                ),
            )
        )
        found: dict[uuid.UUID, int] = {}
        for batch_id, units in rows.tuples():
            assert batch_id is not None
            found[batch_id] = found.get(batch_id, 0) + units
        return found


async def _void_audit(
    session_factory: async_sessionmaker[AsyncSession], scene: Scene
) -> list[BillingAuditEntry]:
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingAuditEntry)
            .where(
                BillingAuditEntry.action == AuditAction.BATCH_TEST_VOID.value,
                BillingAuditEntry.entity_id.in_(
                    [scene.marked, scene.unmarked, scene.author_marked]
                ),
            )
            .order_by(BillingAuditEntry.created_at)
        )
        return list(rows.scalars())


class TestVoid:
    async def test_only_the_free_units_of_marked_batches_go(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        scene: Scene,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = await _void()  # every marked batch there is

        assert code == 0
        out = capsys.readouterr().out
        assert (
            f"account {world.student_account} (student)  batch {scene.marked}  "
            "voided=850  left reserved=100  left held=50"
        ) in out
        assert str(world.tenant_id) not in out  # no owner's id
        assert await _voided(session_factory, world) == {
            scene.marked: 850,
            scene.author_marked: 500,
        }  # the unmarked batch is untouched
        async with session_factory() as session:
            student = await ledger.balance(session, world.student_account)
            author = await ledger.balance(session, world.author_account)
        assert (student.balance, student.reserved, student.held) == (350, 100, 50)
        assert student.available == 200  # the administrator's credit only
        assert (author.balance, author.available) == (0, 0)

    async def test_the_audit_names_the_actor_and_the_free_units(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        scene: Scene,
    ) -> None:
        assert await _void("--account", str(world.student_account)) == 0

        [entry] = await _void_audit(session_factory, scene)
        assert (entry.actor, entry.action, entry.entity, entry.entity_id) == (
            ACTOR,
            "batch.test_void",
            "batch",
            scene.marked,
        )
        assert (entry.before, entry.after) == ({"free_units": 850}, {"free_units": 0})

    async def test_a_second_run_changes_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        scene: Scene,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert await _void("--account", str(world.student_account)) == 0
        voided = await _voided(session_factory, world)
        capsys.readouterr()

        assert await _void("--account", str(world.student_account)) == 0

        out = capsys.readouterr().out
        assert "voided=0  left reserved=100  left held=50" in out
        assert await _voided(session_factory, world) == voided
        assert len(await _void_audit(session_factory, scene)) == 1

    async def test_units_freed_later_go_with_a_later_run(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        scene: Scene,
    ) -> None:
        assert await _void("--account", str(world.student_account)) == 0
        async with session_factory() as session:
            await ledger.release(
                session, action_kind=ActionKind.REVIEW, action_key=scene.action_key
            )
            await session.commit()

        assert await _void("--account", str(world.student_account)) == 0

        movements = await _voided(session_factory, world)
        assert movements == {scene.marked: 950}  # 850, then the 100 released

    async def test_a_dry_run_writes_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        scene: Scene,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = await _void("--account", str(world.student_account), "--dry-run")

        assert code == 0
        out = capsys.readouterr().out
        assert "voided=850  left reserved=100  left held=50" in out
        assert out.rstrip().endswith("dry run: nothing written")
        assert await _voided(session_factory, world) == {}
        assert await _void_audit(session_factory, scene) == []

    async def test_the_statement_shows_the_void_as_an_outflow(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        scene: Scene,
    ) -> None:
        assert await _void("--account", str(world.student_account)) == 0

        async with session_factory() as session:
            page = await statement_page(session, world.student_account, limit=10)
        newest = page.lines[0]
        assert newest.kind is StatementLineKind.TEST_VOIDED
        assert (newest.affects_balance, newest.units) == (True, -850)
        assert newest.balance_after == 1000 + 200 - 850

    async def test_show_account_prints_the_test_units(
        self,
        world: World,
        scene: Scene,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        account = ["show-account", "--account", str(world.student_account)]

        assert await _admin(*account) == 0
        before = capsys.readouterr().out
        assert await _void("--account", str(world.student_account)) == 0
        capsys.readouterr()
        assert await _admin(*account) == 0
        after = capsys.readouterr().out

        assert "test units=850  (free units of 1 batch(es) marked" in before
        assert "test units=0  (free units of 1 batch(es) marked" in after

    async def test_an_account_without_marks_shows_none(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await _admin("show-account", "--account", str(world.author_account)) == 0
        assert "test units=0  (no batch marked" in capsys.readouterr().out


# ── The table ──────────────────────────────────────────────────────────


class TestTable:
    async def test_a_mark_cannot_be_updated(
        self, world: World, db_session: AsyncSession
    ) -> None:
        batch = await ledger.credit_purchase(
            db_session,
            account_id=world.student_account,
            units=10,
            amount_paid_minor=10,
            currency="EUR",
            provider=CREEM,
            provider_transaction_id=_order(),
            provider_line_id=_order(),
        )
        await testmark.mark(
            db_session,
            batch_id=batch.batch.id,
            origin=MarkOrigin.CREDIT,
            now=datetime.now(UTC),
        )
        with pytest.raises(DBAPIError, match="insert-only"):
            async with db_session.begin_nested():
                await db_session.execute(
                    text("UPDATE billing_test_batches SET marked_by = 'backfill'")
                )

    async def test_an_unknown_origin_is_refused(
        self, world: World, db_session: AsyncSession
    ) -> None:
        batch = await ledger.credit_admin(
            db_session,
            account_id=world.student_account,
            units=10,
            reason="r",
            actor=ACTOR,
        )
        with pytest.raises(DBAPIError, match="ck_billing_test_batches_marked_by"):
            async with db_session.begin_nested():
                await db_session.execute(
                    text(
                        "INSERT INTO billing_test_batches (batch_id, marked_by) "
                        "VALUES (:b, 'guess')"
                    ),
                    {"b": batch.batch.id},
                )

    async def test_comments_are_the_orms(self, db_session: AsyncSession) -> None:
        orm = BillingTestBatch.__table__
        rows = (
            await db_session.execute(
                text(
                    "SELECT a.attname, col_description(a.attrelid, a.attnum) "
                    "FROM pg_attribute a WHERE a.attrelid = "
                    "CAST('billing_test_batches' AS regclass) "
                    "AND a.attnum > 0 AND NOT a.attisdropped"
                )
            )
        ).all()
        assert dict(rows) == {c.name: c.comment for c in orm.c}
        comment = await db_session.scalar(
            text("SELECT obj_description(CAST('billing_test_batches' AS regclass))")
        )
        assert comment == orm.comment


# ── The migration ──────────────────────────────────────────────────────


def _engine() -> Any:
    return create_engine(get_settings().database_url)


def _account(conn: Any) -> uuid.UUID:
    account_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO billing_accounts (id, kind, tenant_id) "
            "VALUES (:a, 'author', :t)"
        ),
        {"a": account_id, "t": uuid.uuid4()},
    )
    return account_id


def _purchase(conn: Any, account_id: uuid.UUID, provider: str) -> uuid.UUID:
    batch_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO billing_batches (id, account_id, source, units, "
            "expires_at, amount_paid_minor, currency, provider, "
            "provider_transaction_id, provider_line_id) VALUES (:b, :a, "
            "'purchase', 10, now() + interval '6 months', 500, 'EUR', :p, :n, :n)"
        ),
        {"b": batch_id, "a": account_id, "p": provider, "n": f"n-{batch_id.hex}"},
    )
    return batch_id


def _admin_credit(conn: Any, account_id: uuid.UUID) -> uuid.UUID:
    batch_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO billing_batches (id, account_id, source, units, "
            "expires_at, reason, actor) VALUES (:b, :a, 'admin_credit', 10, "
            "now() + interval '6 months', 'pilot', 'test-admin')"
        ),
        {"b": batch_id, "a": account_id},
    )
    return batch_id


def _forget(account_id: uuid.UUID) -> None:
    engine = _engine()
    with engine.begin() as conn:
        for statement in (
            "DELETE FROM billing_movements WHERE account_id = :a",
            "DELETE FROM billing_test_batches WHERE batch_id IN "
            "(SELECT id FROM billing_batches WHERE account_id = :a)",
            "DELETE FROM billing_batches WHERE account_id = :a",
            "DELETE FROM billing_accounts WHERE id = :a",
        ):
            conn.execute(text(statement), {"a": account_id})
    engine.dispose()


def _head(cfg: Config) -> str | None:
    return ScriptDirectory.from_config(cfg).get_current_head()


def test_the_migration_goes_down_and_up() -> None:
    cfg = Config("alembic.ini")
    engine = _engine()
    kinds = text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_billing_movements_kind'"
    )
    trigger = text(
        "SELECT tgenabled FROM pg_trigger "
        "WHERE tgname = 'billing_test_batches_insert_only'"
    )
    try:
        command.downgrade(cfg, BELOW)
        with engine.connect() as conn:
            assert "billing_test_batches" not in inspect(conn).get_table_names()
            assert "test_voided" not in conn.scalar(kinds)
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert "billing_test_batches" in inspect(conn).get_table_names()
        assert "'test_voided'" in conn.scalar(kinds)
        assert conn.scalar(trigger) == "O"
    engine.dispose()


def test_the_backfill_marks_creem_purchases_only() -> None:
    cfg = Config("alembic.ini")
    engine = _engine()
    command.downgrade(cfg, BELOW)
    try:
        with engine.begin() as conn:
            account_id = _account(conn)
            creem = _purchase(conn, account_id, CREEM)
            invoice = _purchase(conn, account_id, "invoice")
            fake = _purchase(conn, account_id, "fake")
            admin = _admin_credit(conn, account_id)
    finally:
        command.upgrade(cfg, "head")
    try:
        with engine.connect() as conn:
            marks = conn.execute(
                text(
                    "SELECT batch_id, marked_by FROM billing_test_batches "
                    "WHERE batch_id IN (:c, :i, :f, :d)"
                ),
                {"c": creem, "i": invoice, "f": fake, "d": admin},
            ).all()
        assert [tuple(m) for m in marks] == [(creem, "backfill")]
    finally:
        _forget(account_id)
        engine.dispose()


@pytest.mark.parametrize("left", ["mark", "test_voided"])
def test_the_downgrade_is_refused_while_marks_or_voids_exist(left: str) -> None:
    cfg = Config("alembic.ini")
    engine = _engine()
    with engine.begin() as conn:
        account_id = _account(conn)
        batch_id = _purchase(conn, account_id, CREEM)
        if left == "mark":
            conn.execute(
                text(
                    "INSERT INTO billing_test_batches (batch_id, marked_by) "
                    "VALUES (:b, 'credit')"
                ),
                {"b": batch_id},
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO billing_movements (id, account_id, batch_id, kind, "
                    "units, actor) VALUES (:m, :a, :b, 'test_voided', 10, :actor)"
                ),
                {"m": uuid.uuid4(), "a": account_id, "b": batch_id, "actor": ACTOR},
            )
    try:
        with pytest.raises(RuntimeError, match="refusing to downgrade"):
            command.downgrade(cfg, BELOW)
        with engine.connect() as conn:
            current = conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert current == _head(cfg)  # the whole downgrade rolled back
    finally:
        _forget(account_id)
        engine.dispose()
