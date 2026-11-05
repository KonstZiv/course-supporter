"""BIL-B on a live database: the journal of units, point by point of the model.

The model (operator, ratified 2026-11-05) is numbered 1-12 in
``billing/ledger.py``; each class below names the points it proves. Each
test runs in a transaction that is rolled back (``db_session``), except the
concurrency test, which needs two real transactions.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Table, create_engine, delete, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.billing import admin, ledger
from course_supporter.billing.accounts import (
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.benefit import benefit_allowance
from course_supporter.billing.errors import (
    BillingConflictError,
    BillingInvalidError,
    BillingNotFoundError,
)
from course_supporter.billing.expiry import annul_differences, expire_batches
from course_supporter.billing.journal import add_months
from course_supporter.billing.kinds import (
    ActionKind,
    AuditAction,
    Benefit,
    MovementKind,
    PayerSource,
    RuleScope,
)
from course_supporter.billing.refunds import refund_approve, refund_hold, refund_reject
from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingBatch,
    BillingDifference,
    BillingMovement,
    BillingRefund,
    BillingReservation,
    Student,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

LEDGER_TABLES = (
    "billing_batches",
    "billing_refunds",
    "billing_reservations",
    "billing_differences",
    "billing_movements",
)
ACTOR = "test-admin"
T0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
EXPIRY = add_months(T0, 6)  # 2026-07-10 12:00
REVIEW = ActionKind.REVIEW
PROCESSING = ActionKind.PROCESSING


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    course_id: uuid.UUID
    student_id: uuid.UUID
    student: uuid.UUID
    """The student's billing account."""
    author: uuid.UUID
    """The author's (the tenant's) billing account."""


@pytest.fixture()
async def world(db_session: AsyncSession) -> World:
    tenant = Tenant(name=f"bil-b-{uuid.uuid4().hex[:8]}")
    db_session.add(tenant)
    await db_session.flush()
    course = make_root_course_node(tenant_id=tenant.id, title="Курс", order=0)
    student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:8]}")
    db_session.add_all([course, student])
    await db_session.flush()
    student_account = await get_or_create_student_account(
        db_session, tenant_id=tenant.id, student_id=student.id
    )
    author_account = await get_or_create_author_account(db_session, tenant_id=tenant.id)
    return World(
        tenant.id, course.id, student.id, student_account.id, author_account.id
    )


def key() -> str:
    return str(uuid.uuid4())


async def buy(
    session: AsyncSession,
    account_id: uuid.UUID,
    units: int,
    *,
    at: datetime = T0,
    amount: int = 1000,
) -> BillingBatch:
    done = await ledger.credit_purchase(
        session,
        account_id=account_id,
        units=units,
        amount_paid_minor=amount,
        currency="USD",
        now=at,
    )
    return done.batch


async def end(
    session: AsyncSession,
    action_key: str,
    fact: int,
    *,
    payer: uuid.UUID,
    beneficiary: uuid.UUID | None = None,
    kind: ActionKind = REVIEW,
    source: PayerSource = PayerSource.DEFAULT,
    rule_id: uuid.UUID | None = None,
    at: datetime = T0,
    without_reserve: bool = False,
) -> ledger.Settlement:
    return await ledger.settle(
        session,
        action_kind=kind,
        action_key=action_key,
        fact_units=fact,
        beneficiary_account_id=beneficiary or payer,
        payer_source=source,
        rule_id=rule_id,
        account_id=payer if without_reserve else None,
        now=at,
    )


async def hold(
    session: AsyncSession,
    account_id: uuid.UUID,
    units: int,
    *,
    kind: ActionKind = REVIEW,
    at: datetime = T0,
) -> tuple[str, BillingReservation]:
    action_key = key()
    reservation = await ledger.reserve(
        session,
        action_kind=kind,
        action_key=action_key,
        account_id=account_id,
        units=units,
        now=at,
    )
    return action_key, reservation


async def bal(
    session: AsyncSession, account_id: uuid.UUID, at: datetime = T0
) -> ledger.Balance:
    view = await ledger.balance(session, account_id, now=at)
    # Model 5: a difference and available units never coexist.
    assert not (view.difference and view.available), view
    return view


async def journal(
    session: AsyncSession, account_id: uuid.UUID
) -> list[tuple[str, int]]:
    return [(m.kind, m.units) for m in await ledger.statement(session, account_id)]


async def movement_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(text("SELECT count(*) FROM billing_movements")) or 0
    )


async def grant(
    session: AsyncSession,
    account_id: uuid.UUID,
    at: datetime,
    benefit: Benefit,
    limit: int | None = None,
) -> None:
    """An ``author_settings.update`` audit entry at ``at`` (the benefit's history)."""
    session.add(
        BillingAuditEntry(
            actor=ACTOR,
            created_at=at,
            action=AuditAction.AUTHOR_SETTINGS_UPDATE.value,
            entity="author_settings",
            entity_id=account_id,
            before=None,
            after={"benefit": benefit.value, "monthly_limit_units": limit},
        )
    )
    await session.flush()


# ── Credits (models 1, 7, 9) ───────────────────────────────────────────


class TestCredits:
    async def test_a_purchase_is_a_batch_valid_six_months(
        self, db_session: AsyncSession, world: World
    ) -> None:
        batch = await buy(db_session, world.student, 100)
        assert (batch.source, batch.units, batch.created_at) == ("purchase", 100, T0)
        assert batch.expires_at == datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
        view = await bal(db_session, world.student)
        assert (view.balance, view.available, view.next_expiry) == (100, 100, EXPIRY)
        assert await journal(db_session, world.student) == [("credit", 100)]

    async def test_a_purchase_is_idempotent_by_its_paddle_line(
        self, db_session: AsyncSession, world: World
    ) -> None:
        args: dict[str, Any] = {
            "account_id": world.student,
            "units": 100,
            "amount_paid_minor": 1999,
            "currency": "EUR",
            "paddle_transaction_id": "txn_1",
            "paddle_line_id": "txnitm_1",
            "now": T0,
        }
        first = await ledger.credit_purchase(db_session, **args)
        again = await ledger.credit_purchase(db_session, **args)
        assert (first.created, again.created) == (True, False)
        assert again.batch.id == first.batch.id
        assert (await bal(db_session, world.student)).balance == 100
        with pytest.raises(BillingConflictError, match="other figures"):
            await ledger.credit_purchase(db_session, **{**args, "units": 200})
        with pytest.raises(BillingInvalidError, match="both Paddle ids"):
            await ledger.credit_purchase(db_session, **{**args, "paddle_line_id": None})

    async def test_an_admin_credit_is_a_batch_with_a_reason_and_an_audit_entry(
        self, db_session: AsyncSession, world: World
    ) -> None:
        done = await ledger.credit_admin(
            db_session,
            account_id=world.author,
            units=500,
            reason=" pilot ",
            actor=ACTOR,
            now=T0,
        )
        batch = done.batch
        assert (batch.source, batch.reason, batch.actor) == (
            "admin_credit",
            "pilot",
            ACTOR,
        )
        assert batch.expires_at == EXPIRY
        assert batch.amount_paid_minor is None
        [entry] = (
            await db_session.execute(
                select(BillingAuditEntry).where(BillingAuditEntry.entity_id == batch.id)
            )
        ).scalars()
        assert (entry.actor, entry.action, entry.entity) == (
            ACTOR,
            "batch.admin_credit",
            "batch",
        )
        assert entry.after is not None and entry.after["units"] == 500
        [movement] = await ledger.statement(db_session, world.author)
        assert (movement.kind, movement.units, movement.actor) == ("credit", 500, ACTOR)

    @pytest.mark.parametrize(
        ("units", "reason", "actor", "message"),
        [
            (10, " ", ACTOR, "needs a reason"),
            (10, "why", " ", "needs an actor"),
            (0, "why", ACTOR, "positive"),
        ],
    )
    async def test_an_admin_credit_needs_reason_actor_units(
        self,
        db_session: AsyncSession,
        world: World,
        units: int,
        reason: str,
        actor: str,
        message: str,
    ) -> None:
        with pytest.raises(BillingInvalidError, match=message):
            await ledger.credit_admin(
                db_session,
                account_id=world.author,
                units=units,
                reason=reason,
                actor=actor,
            )
        assert await journal(db_session, world.author) == []

    async def test_a_closed_account_gets_nothing_new(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await db_session.execute(
            text(
                "UPDATE billing_accounts SET status = 'closed', closed_at = now() "
                "WHERE id = :id"
            ),
            {"id": world.student},
        )
        with pytest.raises(BillingConflictError, match="closed"):
            await buy(db_session, world.student, 10)
        with pytest.raises(BillingConflictError, match="closed"):
            await hold(db_session, world.student, 10)


# ── Reserve (models 1, 2) ──────────────────────────────────────────────


class TestReserve:
    async def test_units_come_from_the_batch_that_expires_first(
        self, db_session: AsyncSession, world: World
    ) -> None:
        first = await buy(db_session, world.student, 50)
        second = await buy(db_session, world.student, 50, at=T0 + timedelta(days=30))
        _, reservation = await hold(db_session, world.student, 70)
        assert reservation.units == 70
        assert reservation.allocation == [
            {"batch_id": str(first.id), "units": 50},
            {"batch_id": str(second.id), "units": 20},
        ]
        movements = await ledger.statement(db_session, world.student)
        assert [
            (m.kind, m.batch_id, m.units) for m in movements if m.kind == "reserve"
        ] == [
            ("reserve", first.id, 50),
            ("reserve", second.id, 20),
        ]

    async def test_a_reserve_is_capped_by_the_available_units(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 30)
        _, reservation = await hold(db_session, world.student, 100)
        assert reservation.units == 30
        _, empty = await hold(db_session, world.student, 50)
        assert (empty.units, empty.allocation) == (0, [])

    async def test_available_is_the_balance_less_the_other_reserves(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        _, a = await hold(db_session, world.student, 60)
        _, b = await hold(db_session, world.student, 60)
        assert (a.units, b.units) == (60, 40)
        view = await bal(db_session, world.student)
        assert (view.balance, view.reserved, view.available) == (100, 100, 0)

    async def test_a_repeated_reserve_returns_the_one_there_is(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        action_key, first = await hold(db_session, world.student, 40)
        before = await movement_count(db_session)
        again = await ledger.reserve(
            db_session,
            action_kind=REVIEW,
            action_key=action_key,
            account_id=world.student,
            units=90,
        )
        assert (again.id, again.units) == (first.id, 40)
        assert await movement_count(db_session) == before
        with pytest.raises(BillingConflictError, match="another account"):
            await ledger.reserve(
                db_session,
                action_kind=REVIEW,
                action_key=action_key,
                account_id=world.author,
                units=10,
            )

    async def test_a_negative_reserve_is_refused(
        self, db_session: AsyncSession, world: World
    ) -> None:
        with pytest.raises(BillingInvalidError, match="negative"):
            await hold(db_session, world.student, -1)


# ── Settle and release (models 3, 4, 6) ────────────────────────────────


class TestSettle:
    async def test_a_fact_below_the_reserve_releases_the_rest(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        action_key, _ = await hold(db_session, world.student, 60)
        done = await end(db_session, action_key, 25, payer=world.student)
        assert (done.charged_from_reserve, done.charged_from_available) == (25, 0)
        assert (done.released_units, done.difference_units) == (35, 0)
        view = await bal(db_session, world.student)
        assert (view.balance, view.reserved, view.available) == (75, 0, 75)

    async def test_a_fact_above_the_reserve_takes_the_available_units(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        action_key, _ = await hold(db_session, world.student, 30)
        done = await end(db_session, action_key, 50, payer=world.student)
        assert (done.charged_from_reserve, done.charged_from_available) == (30, 20)
        assert done.released_units == 0
        assert (await bal(db_session, world.student)).balance == 50

    async def test_what_is_not_there_is_a_difference(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 40)
        action_key, _ = await hold(db_session, world.student, 30)
        done = await end(db_session, action_key, 70, payer=world.student)
        assert (done.charged_units, done.difference_units) == (40, 30)
        difference = await db_session.get(BillingDifference, done.difference_id)
        assert difference is not None
        assert (difference.state, difference.outstanding_units) == ("open", 30)
        assert difference.annul_after == EXPIRY  # six months after it arose
        view = await bal(db_session, world.student)
        assert (view.balance, view.available, view.difference) == (0, 0, 30)

    async def test_an_author_allowed_credit_owes_without_a_term(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await admin.update_author_settings(
            db_session, world.tenant_id, actor=ACTOR, credit_allowed=True
        )
        done = await end(
            db_session,
            key(),
            50,
            payer=world.author,
            kind=PROCESSING,
            without_reserve=True,
        )
        difference = await db_session.get(BillingDifference, done.difference_id)
        assert difference is not None and difference.annul_after is None

    async def test_a_failure_releases_everything_and_charges_nothing(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        action_key, _ = await hold(db_session, world.student, 60)
        done = await ledger.release(
            db_session, action_kind=REVIEW, action_key=action_key, now=T0
        )
        assert (done.released_units, done.repaid_units) == (60, 0)
        view = await bal(db_session, world.student)
        assert (view.balance, view.available) == (100, 100)
        assert "debit" not in {k for k, _ in await journal(db_session, world.student)}

    async def test_without_a_reserve_the_payer_is_needed(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        action_key = key()
        with pytest.raises(BillingNotFoundError, match="account_id"):
            await end(db_session, action_key, 10, payer=world.student)
        done = await end(
            db_session, action_key, 10, payer=world.student, without_reserve=True
        )
        assert (done.reserved_units, done.charged_from_available) == (0, 10)
        reservation = await db_session.get(BillingReservation, done.reservation_id)
        assert reservation is not None
        assert (reservation.state, reservation.units, reservation.fact_units) == (
            "settled",
            0,
            10,
        )

    async def test_settle_and_release_are_idempotent(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        settled, _ = await hold(db_session, world.student, 60)
        first = await end(db_session, settled, 25, payer=world.student)
        count = await movement_count(db_session)
        assert await end(db_session, settled, 25, payer=world.student) == first
        assert await movement_count(db_session) == count
        with pytest.raises(BillingConflictError, match="settled with 25"):
            await end(db_session, settled, 26, payer=world.student)
        with pytest.raises(BillingConflictError, match="cannot be released"):
            await ledger.release(
                db_session, action_kind=REVIEW, action_key=settled, now=T0
            )

        failed, _ = await hold(db_session, world.student, 30)
        once = await ledger.release(
            db_session, action_kind=REVIEW, action_key=failed, now=T0
        )
        count = await movement_count(db_session)
        twice = await ledger.release(
            db_session, action_kind=REVIEW, action_key=failed, now=T0
        )
        assert twice == once
        assert await movement_count(db_session) == count
        with pytest.raises(BillingConflictError, match="released"):
            await end(db_session, failed, 5, payer=world.student)
        with pytest.raises(BillingNotFoundError, match="no reserve"):
            await ledger.release(db_session, action_kind=REVIEW, action_key=key())
        assert (await bal(db_session, world.student)).balance == 75

    async def test_the_movements_say_for_whom_and_why(
        self, db_session: AsyncSession, world: World
    ) -> None:
        rule = await admin.create_payment_rule(
            db_session,
            actor=ACTOR,
            tenant_id=world.tenant_id,
            scope=RuleScope.COURSE,
            action_kind=REVIEW,
            course_node_id=world.course_id,
        )
        await buy(db_session, world.author, 100)
        action_key, _ = await hold(db_session, world.author, 50)
        await end(
            db_session,
            action_key,
            20,
            payer=world.author,
            beneficiary=world.student,
            source=PayerSource.COURSE_RULE,
            rule_id=rule.id,
        )
        [debit] = [
            m
            for m in await ledger.statement(db_session, world.author)
            if m.kind == "debit"
        ]
        assert (debit.action_kind, debit.action_key, debit.units) == (
            "review",
            action_key,
            20,
        )
        assert (debit.beneficiary_account_id, debit.payer_source, debit.rule_id) == (
            world.student,
            "course_rule",
            rule.id,
        )

    async def test_the_benefit_is_not_the_callers_to_pass(
        self, db_session: AsyncSession, world: World
    ) -> None:
        with pytest.raises(BillingInvalidError, match="benefit"):
            await end(
                db_session, key(), 1, payer=world.author, source=PayerSource.BENEFIT
            )


# ── Difference (model 5) ───────────────────────────────────────────────


async def owe(
    session: AsyncSession, account_id: uuid.UUID, units: int, *, at: datetime = T0
) -> ledger.Settlement:
    """An action charged without a reserve while nothing is available."""
    done = await end(
        session, key(), units, payer=account_id, at=at, without_reserve=True
    )
    assert done.difference_units == units
    return done


class TestDifference:
    async def test_a_purchase_repays_the_difference_first(
        self, db_session: AsyncSession, world: World
    ) -> None:
        owed = await owe(db_session, world.student, 30)
        done = await ledger.credit_purchase(
            db_session,
            account_id=world.student,
            units=100,
            amount_paid_minor=1000,
            currency="USD",
            now=T0 + timedelta(days=1),
        )
        assert done.repaid_units == 30
        view = await bal(db_session, world.student, T0 + timedelta(days=1))
        assert (view.balance, view.difference) == (70, 0)
        difference = await db_session.get(BillingDifference, owed.difference_id)
        assert difference is not None
        await db_session.refresh(difference)
        assert (difference.state, difference.outstanding_units) == ("repaid", 0)

    async def test_the_oldest_difference_is_repaid_first(
        self, db_session: AsyncSession, world: World
    ) -> None:
        old = await owe(db_session, world.student, 30)
        new = await owe(db_session, world.student, 20, at=T0 + timedelta(days=1))
        await buy(db_session, world.student, 40, at=T0 + timedelta(days=2))
        states = {}
        for done in (old, new):
            row = await db_session.get(BillingDifference, done.difference_id)
            assert row is not None
            await db_session.refresh(row)
            states[done.difference_id] = (row.state, row.outstanding_units)
        assert states == {
            old.difference_id: ("repaid", 0),
            new.difference_id: ("open", 10),
        }
        view = await bal(db_session, world.student, T0 + timedelta(days=2))
        assert (view.available, view.difference) == (0, 10)

    async def test_an_admin_credit_repays_the_difference(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await owe(db_session, world.author, 30)
        done = await ledger.credit_admin(
            db_session,
            account_id=world.author,
            units=10,
            reason="r",
            actor=ACTOR,
            now=T0,
        )
        assert done.repaid_units == 10
        view = await bal(db_session, world.author)
        assert (view.balance, view.difference) == (0, 20)

    async def test_the_released_rest_of_another_actions_reserve_repays(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        first, _ = await hold(db_session, world.student, 100)  # all of it
        await owe(db_session, world.student, 40)  # a second action: nothing left
        done = await end(db_session, first, 30, payer=world.student)
        assert (done.released_units, done.repaid_units) == (70, 40)
        view = await bal(db_session, world.student)
        assert (view.balance, view.available, view.difference) == (30, 30, 0)
        repaid = [
            m
            for m in await ledger.statement(db_session, world.student)
            if m.kind == "difference_repaid"
        ]
        assert [(m.units, m.action_key) for m in repaid] == [(40, first)]

    async def test_a_failure_release_repays_the_difference(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        first, _ = await hold(db_session, world.student, 100)
        await owe(db_session, world.student, 40)
        done = await ledger.release(
            db_session, action_kind=REVIEW, action_key=first, now=T0
        )
        assert (done.released_units, done.repaid_units) == (100, 40)
        view = await bal(db_session, world.student)
        assert (view.balance, view.available, view.difference) == (60, 60, 0)


# ── Benefit (model 10) ─────────────────────────────────────────────────


JAN_1 = datetime(2026, 1, 1, tzinfo=UTC)


class TestBenefit:
    async def test_unlimited_covers_the_authors_processing(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await grant(db_session, world.author, JAN_1, Benefit.UNLIMITED)
        await buy(db_session, world.author, 10)
        action_key, _ = await hold(db_session, world.author, 20, kind=PROCESSING)
        done = await end(
            db_session, action_key, 50, payer=world.author, kind=PROCESSING
        )
        assert (done.benefit_units, done.charged_units, done.released_units) == (
            50,
            0,
            10,
        )
        [covered] = [
            m
            for m in await ledger.statement(db_session, world.author)
            if m.kind == "benefit_covered"
        ]
        assert (covered.units, covered.payer_source, covered.batch_id) == (
            50,
            "benefit",
            None,
        )
        assert (await bal(db_session, world.author)).balance == 10

    async def test_the_monthly_limit_is_split_at_its_edge(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await grant(db_session, world.author, JAN_1, Benefit.MONTHLY_LIMIT, 310)
        await buy(db_session, world.author, 100)
        first = await end(
            db_session,
            key(),
            280,
            payer=world.author,
            kind=PROCESSING,
            without_reserve=True,
        )
        second = await end(
            db_session,
            key(),
            50,
            payer=world.author,
            kind=ActionKind.TASK_PREPARATION,
            at=T0 + timedelta(hours=1),
            without_reserve=True,
        )
        assert (first.benefit_units, first.charged_units) == (280, 0)
        assert (second.benefit_units, second.charged_units) == (30, 20)
        allowance = await benefit_allowance(
            db_session, world.author, T0.date(), now=T0 + timedelta(hours=2)
        )
        assert (
            allowance.limit_units,
            allowance.used_units,
            allowance.remaining_units,
        ) == (
            310,
            310,
            0,
        )
        assert (
            await bal(db_session, world.author, T0 + timedelta(hours=2))
        ).balance == 80

    async def test_a_review_the_author_pays_for_is_never_covered(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await grant(db_session, world.author, JAN_1, Benefit.UNLIMITED)
        await buy(db_session, world.author, 100)
        done = await end(
            db_session,
            key(),
            30,
            payer=world.author,
            beneficiary=world.student,
            source=PayerSource.COURSE_RULE,
            without_reserve=True,
        )
        assert (done.benefit_units, done.charged_units) == (0, 30)

    async def test_a_student_is_never_covered(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await grant(db_session, world.student, JAN_1, Benefit.UNLIMITED)  # nonsense row
        await buy(db_session, world.student, 100)
        done = await end(
            db_session,
            key(),
            30,
            payer=world.student,
            kind=PROCESSING,
            without_reserve=True,
        )
        assert done.benefit_units == 0

    async def test_platform_reprocessing_takes_nothing_and_uses_no_limit(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await grant(db_session, world.author, JAN_1, Benefit.MONTHLY_LIMIT, 100)
        await buy(db_session, world.author, 100)
        action_key, _ = await hold(db_session, world.author, 20, kind=PROCESSING)
        done = await end(
            db_session,
            action_key,
            70,
            payer=world.author,
            kind=PROCESSING,
            source=PayerSource.PLATFORM,
        )
        assert (done.platform_units, done.benefit_units, done.charged_units) == (
            70,
            0,
            0,
        )
        assert done.released_units == 20
        allowance = await benefit_allowance(db_session, world.author, T0.date(), now=T0)
        assert allowance.used_units == 0
        assert (await bal(db_session, world.author)).balance == 100

    async def test_the_first_month_is_prorated_from_the_day_granted(
        self, db_session: AsyncSession, world: World
    ) -> None:
        granted = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
        await grant(db_session, world.author, granted, Benefit.MONTHLY_LIMIT, 300)
        later = datetime(2026, 12, 2, tzinfo=UTC)
        november = await benefit_allowance(
            db_session, world.author, granted.date(), now=later
        )
        december = await benefit_allowance(
            db_session, world.author, later.date(), now=later
        )
        assert november.limit_units == 260  # 300 x 26 / 30: the 5th counts
        assert december.limit_units == 300

    async def test_a_change_mid_month_is_prorated_rounded_down(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await grant(
            db_session,
            world.author,
            datetime(2025, 12, 15, tzinfo=UTC),
            Benefit.MONTHLY_LIMIT,
            100,
        )
        # Two changes on 11 January: the last one of the day takes the day.
        await grant(
            db_session,
            world.author,
            datetime(2026, 1, 11, 9, 0, tzinfo=UTC),
            Benefit.MONTHLY_LIMIT,
            150,
        )
        await grant(
            db_session,
            world.author,
            datetime(2026, 1, 11, 10, 0, tzinfo=UTC),
            Benefit.MONTHLY_LIMIT,
            200,
        )
        allowance = await benefit_allowance(
            db_session, world.author, T0.date(), now=datetime(2026, 2, 1, tzinfo=UTC)
        )
        # (10 x 100 + 21 x 200) / 31 = 5200 / 31 = 167.7 -> 167
        assert allowance.limit_units == 167

    async def test_units_covered_on_unlimited_days_are_not_the_limits(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await grant(db_session, world.author, JAN_1, Benefit.UNLIMITED)
        await end(
            db_session,
            key(),
            50,
            payer=world.author,
            kind=PROCESSING,
            at=datetime(2026, 1, 5, tzinfo=UTC),
            without_reserve=True,
        )
        await grant(
            db_session,
            world.author,
            datetime(2026, 1, 11, tzinfo=UTC),
            Benefit.MONTHLY_LIMIT,
            310,
        )
        allowance = await benefit_allowance(
            db_session, world.author, T0.date(), now=datetime(2026, 1, 12, tzinfo=UTC)
        )
        assert (allowance.limit_units, allowance.used_units) == (
            210,
            0,
        )  # 21 x 310 / 31

    async def test_no_benefit_once_withdrawn(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await grant(db_session, world.author, JAN_1, Benefit.MONTHLY_LIMIT, 300)
        await grant(
            db_session, world.author, datetime(2026, 1, 11, tzinfo=UTC), Benefit.NONE
        )
        await buy(db_session, world.author, 100)
        done = await end(
            db_session,
            key(),
            30,
            payer=world.author,
            kind=PROCESSING,
            at=datetime(2026, 1, 12, tzinfo=UTC),
            without_reserve=True,
        )
        assert (done.benefit_units, done.charged_units) == (0, 30)


# ── Expiry (models 7, 8) ───────────────────────────────────────────────


class TestExpiry:
    async def test_free_units_of_an_expired_batch_burn_once(
        self, db_session: AsyncSession, world: World
    ) -> None:
        batch = await buy(db_session, world.student, 100)
        after = EXPIRY + timedelta(seconds=1)
        assert (
            await bal(db_session, world.student, EXPIRY - timedelta(seconds=1))
        ).available == 100
        assert (await bal(db_session, world.student, after)).balance == 0
        burned = await expire_batches(db_session, after)
        assert [(b.batch_id, b.units) for b in burned] == [(batch.id, 100)]
        assert await expire_batches(db_session, after) == []
        assert await journal(db_session, world.student) == [
            ("credit", 100),
            ("batch_expired", 100),
        ]

    async def test_a_reserve_on_an_expired_batch_waits_for_its_action(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        action_key, _ = await hold(
            db_session, world.student, 60, at=T0 + timedelta(days=1)
        )
        after = EXPIRY + timedelta(hours=1)
        view = await bal(db_session, world.student, after)
        assert (view.balance, view.reserved, view.available) == (60, 60, 0)

        [burned] = await expire_batches(db_session, after)
        assert burned.units == 40  # the free part only

        done = await end(db_session, action_key, 25, payer=world.student, at=after)
        assert (done.charged_from_reserve, done.released_units) == (25, 35)
        assert (await bal(db_session, world.student, after)).balance == 0
        [rest] = await expire_batches(db_session, after)
        assert rest.units == 35  # what the action left burns after it
        assert await expire_batches(db_session, after) == []

    async def test_released_units_of_an_expired_batch_never_repay(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        first, _ = await hold(db_session, world.student, 100)
        await owe(db_session, world.student, 30)
        after = EXPIRY + timedelta(hours=1)
        done = await end(db_session, first, 10, payer=world.student, at=after)
        assert (done.released_units, done.repaid_units) == (90, 0)
        view = await bal(db_session, world.student, after)
        assert (view.balance, view.difference) == (0, 30)
        [burned] = await expire_batches(db_session, after)
        assert burned.units == 90

    async def test_the_next_batch_pays_once_the_first_expired(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 50)
        second = await buy(db_session, world.student, 50, at=T0 + timedelta(days=60))
        _, reservation = await hold(
            db_session, world.student, 80, at=EXPIRY + timedelta(days=1)
        )
        assert reservation.allocation == [{"batch_id": str(second.id), "units": 50}]

    async def test_a_difference_is_annulled_after_six_months(
        self, db_session: AsyncSession, world: World
    ) -> None:
        owed = await owe(db_session, world.student, 30)
        await buy(db_session, world.student, 10, at=T0 + timedelta(days=1))  # repays 10
        assert await annul_differences(db_session, EXPIRY - timedelta(seconds=1)) == []
        [annulled] = await annul_differences(db_session, EXPIRY)
        assert (annulled.difference_id, annulled.units) == (owed.difference_id, 20)
        assert await annul_differences(db_session, EXPIRY) == []
        difference = await db_session.get(BillingDifference, owed.difference_id)
        assert difference is not None
        assert (difference.state, difference.outstanding_units) == ("annulled", 0)
        assert (await bal(db_session, world.student, EXPIRY)).difference == 0
        assert ("difference_annulled", 20) in await journal(db_session, world.student)

    async def test_an_author_allowed_credit_keeps_owing(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await admin.update_author_settings(
            db_session, world.tenant_id, actor=ACTOR, credit_allowed=True
        )
        await owe(db_session, world.author, 30)
        assert await annul_differences(db_session, add_months(T0, 60)) == []
        assert (await bal(db_session, world.author)).difference == 30


# ── Refunds (model 11) ─────────────────────────────────────────────────


class TestRefunds:
    async def test_held_units_are_blocked_then_leave_on_approval(
        self, db_session: AsyncSession, world: World
    ) -> None:
        batch = await buy(db_session, world.student, 100, amount=1999)
        refund = await refund_hold(
            db_session,
            account_id=world.student,
            batch_id=batch.id,
            units=40,
            paddle_adjustment_id="adj_1",
            now=T0,
        )
        assert (refund.state, refund.amount_minor, refund.currency) == (
            "held",
            799,
            "USD",
        )
        view = await bal(db_session, world.student)
        assert (view.balance, view.held, view.available) == (100, 40, 60)
        _, reservation = await hold(db_session, world.student, 100)
        assert reservation.units == 60  # held units are not spent

        approved = await refund_approve(
            db_session, paddle_adjustment_id="adj_1", now=T0
        )
        assert approved.state == "approved"
        view = await bal(db_session, world.student)
        assert (view.balance, view.held, view.available) == (60, 0, 0)
        again = await refund_approve(db_session, paddle_adjustment_id="adj_1", now=T0)
        assert again.decided_at == approved.decided_at
        with pytest.raises(BillingConflictError, match="already approved"):
            await refund_reject(db_session, paddle_adjustment_id="adj_1", now=T0)

    async def test_rejected_units_return_and_repay_the_difference(
        self, db_session: AsyncSession, world: World
    ) -> None:
        batch = await buy(db_session, world.student, 100)
        await refund_hold(
            db_session,
            account_id=world.student,
            batch_id=batch.id,
            units=100,
            paddle_adjustment_id="adj_2",
            now=T0,
        )
        await owe(db_session, world.student, 30)  # nothing available while held
        rejected = await refund_reject(db_session, paddle_adjustment_id="adj_2", now=T0)
        assert rejected.state == "rejected"
        view = await bal(db_session, world.student)
        assert (view.balance, view.held, view.available, view.difference) == (
            70,
            0,
            70,
            0,
        )

    async def test_reserved_units_are_not_refunded(
        self, db_session: AsyncSession, world: World
    ) -> None:
        batch = await buy(db_session, world.student, 100)
        await hold(db_session, world.student, 80)
        with pytest.raises(BillingConflictError, match="20 free units"):
            await refund_hold(
                db_session,
                account_id=world.student,
                batch_id=batch.id,
                units=30,
                paddle_adjustment_id="adj_3",
                now=T0,
            )

    async def test_an_admin_credit_is_never_refunded(
        self, db_session: AsyncSession, world: World
    ) -> None:
        done = await ledger.credit_admin(
            db_session,
            account_id=world.author,
            units=100,
            reason="r",
            actor=ACTOR,
            now=T0,
        )
        with pytest.raises(BillingInvalidError, match="never refunded"):
            await refund_hold(
                db_session,
                account_id=world.author,
                batch_id=done.batch.id,
                units=10,
                paddle_adjustment_id="adj_4",
                now=T0,
            )

    async def test_an_expired_batch_has_nothing_to_refund(
        self, db_session: AsyncSession, world: World
    ) -> None:
        batch = await buy(db_session, world.student, 100)
        with pytest.raises(BillingConflictError, match="0 free units"):
            await refund_hold(
                db_session,
                account_id=world.student,
                batch_id=batch.id,
                units=10,
                paddle_adjustment_id="adj_5",
                now=EXPIRY,
            )

    async def test_one_refund_per_paddle_adjustment(
        self, db_session: AsyncSession, world: World
    ) -> None:
        batch = await buy(db_session, world.student, 100)
        args: dict[str, Any] = {
            "account_id": world.student,
            "batch_id": batch.id,
            "units": 10,
            "paddle_adjustment_id": "adj_6",
            "now": T0,
        }
        first = await refund_hold(db_session, **args)
        count = await movement_count(db_session)
        assert (await refund_hold(db_session, **args)).id == first.id
        assert await movement_count(db_session) == count
        with pytest.raises(BillingConflictError, match="other figures"):
            await refund_hold(db_session, **{**args, "units": 11})
        with pytest.raises(BillingNotFoundError):
            await refund_approve(db_session, paddle_adjustment_id="adj_none", now=T0)


# ── Statement ──────────────────────────────────────────────────────────


class TestStatement:
    async def test_a_statement_lists_the_movements_of_a_period(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await buy(db_session, world.student, 100)
        action_key, _ = await hold(
            db_session, world.student, 50, at=T0 + timedelta(days=1)
        )
        await end(
            db_session, action_key, 20, payer=world.student, at=T0 + timedelta(days=2)
        )
        rows = await ledger.statement(
            db_session,
            world.student,
            since=T0 + timedelta(days=1),
            until=T0 + timedelta(days=3),
        )
        assert [(m.kind, m.units) for m in rows] == [
            ("reserve", 50),
            ("debit", 20),
            ("reserve_release", 30),
        ]
        debit = rows[1]
        assert (debit.action_kind, debit.action_key, debit.payer_source) == (
            "review",
            action_key,
            "default",
        )
        assert debit.beneficiary_account_id == world.student


# ── Concurrency ────────────────────────────────────────────────────────


@pytest.fixture()
async def committed_account(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[uuid.UUID]:
    """A tenant's author account with one batch of 100 units, committed."""
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-b-race-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        account = await get_or_create_author_account(session, tenant_id=tenant.id)
        await buy(session, account.id, 100, at=datetime.now(UTC))
        await session.commit()
        tenant_id, account_id = tenant.id, account.id

    yield account_id

    async with session_factory() as session:
        for model in (BillingMovement, BillingReservation, BillingBatch):
            await session.execute(delete(model).where(model.account_id == account_id))
        await session.execute(
            delete(BillingAccount).where(BillingAccount.id == account_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


class TestConcurrency:
    async def test_two_actions_at_once_never_spend_the_same_units(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_account: uuid.UUID,
    ) -> None:
        """Session A reserves 70 and holds the account's lock; session B's
        reserve of 70 waits for it, then sees what A left (30)."""

        async def reserve_in(session: AsyncSession, units: int) -> BillingReservation:
            return await ledger.reserve(
                session,
                action_kind=PROCESSING,
                action_key=key(),
                account_id=committed_account,
                units=units,
            )

        async with session_factory() as a, session_factory() as b:
            first = await reserve_in(a, 70)
            second = asyncio.create_task(reserve_in(b, 70))
            await asyncio.sleep(0.5)
            assert not second.done()  # B waits on A's lock of the account
            await a.commit()
            got = await asyncio.wait_for(second, timeout=10)
            await b.commit()
        assert (first.units, got.units) == (70, 30)


# ── Locks: journal and schema (models 12) ──────────────────────────────


async def _journal_for_both(session: AsyncSession, world: World) -> None:
    await buy(session, world.student, 100)
    action_key, _ = await hold(session, world.student, 50)
    await end(session, action_key, 70, payer=world.student)
    await owe(session, world.author, 30)
    batch = await buy(session, world.author, 100)
    await refund_hold(
        session,
        account_id=world.author,
        batch_id=batch.id,
        units=10,
        paddle_adjustment_id=f"adj-{uuid.uuid4().hex[:8]}",
        now=T0,
    )


async def _counts(session: AsyncSession) -> dict[str, int]:
    return {
        table: int(
            await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0  # noqa: S608
        )
        for table in LEDGER_TABLES
    }


class TestLocks:
    @pytest.mark.parametrize("table", ["billing_batches", "billing_movements"])
    async def test_insert_only(
        self, db_session: AsyncSession, world: World, table: str
    ) -> None:
        await _journal_for_both(db_session, world)
        with pytest.raises(DBAPIError, match="insert-only"):
            async with db_session.begin_nested():
                await db_session.execute(text(f"UPDATE {table} SET id = id"))  # noqa: S608

    async def test_deleting_the_owners_never_touches_the_journal(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await _journal_for_both(db_session, world)
        before = await _counts(db_session)
        assert all(before.values()), before
        await db_session.execute(
            text("DELETE FROM students WHERE id = :id"), {"id": world.student_id}
        )
        await db_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": world.tenant_id}
        )
        assert await db_session.get(Tenant, world.tenant_id) is None
        assert await _counts(db_session) == before

    async def test_the_journals_keys_stay_inside_billing(
        self, db_session: AsyncSession
    ) -> None:
        rows = (
            await db_session.execute(
                text(
                    "SELECT conrelid::regclass::text, confrelid::regclass::text, "
                    "confdeltype FROM pg_constraint WHERE contype = 'f' AND "
                    "conrelid::regclass::text = ANY(:tables)"
                ),
                {"tables": list(LEDGER_TABLES)},
            )
        ).all()
        parents = {(child, parent) for child, parent, _ in rows}
        assert parents == {
            ("billing_batches", "billing_accounts"),
            ("billing_refunds", "billing_accounts"),
            ("billing_refunds", "billing_batches"),
            ("billing_reservations", "billing_accounts"),
            ("billing_differences", "billing_accounts"),
            ("billing_movements", "billing_accounts"),
            ("billing_movements", "billing_batches"),
            ("billing_movements", "billing_differences"),
            ("billing_movements", "billing_refunds"),
            ("billing_movements", "billing_payment_rules"),
        }
        assert {on_delete for _, _, on_delete in rows} == {"a"}  # NO ACTION

    @pytest.mark.parametrize("table", LEDGER_TABLES)
    async def test_comments_are_the_orms(
        self, db_session: AsyncSession, table: str
    ) -> None:
        orm: Table = {
            "billing_batches": BillingBatch,
            "billing_refunds": BillingRefund,
            "billing_reservations": BillingReservation,
            "billing_differences": BillingDifference,
            "billing_movements": BillingMovement,
        }[table].__table__  # type: ignore[assignment]
        rows = (
            await db_session.execute(
                text(
                    "SELECT a.attname, col_description(a.attrelid, a.attnum) "
                    "FROM pg_attribute a WHERE a.attrelid = CAST(:t AS regclass) "
                    "AND a.attnum > 0 AND NOT a.attisdropped"
                ),
                {"t": table},
            )
        ).all()
        live: dict[str, str | None] = {name: comment for name, comment in rows}
        assert live == {c.name: c.comment for c in orm.c}
        comment = await db_session.scalar(
            text("SELECT obj_description(CAST(:t AS regclass))"), {"t": table}
        )
        assert comment == orm.comment

    async def test_every_movement_kind_is_allowed_by_the_table(
        self, db_session: AsyncSession
    ) -> None:
        check = await db_session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_billing_movements_kind'"
            )
        )
        assert check is not None
        assert all(f"'{k.value}'" in check for k in MovementKind)


def test_the_migration_goes_down_and_up() -> None:
    """Downgrade drops the five tables and keeps BIL-A's guard function and
    tables; upgrade brings them back."""
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    guard = text("SELECT count(*) FROM pg_proc WHERE proname = 'billing_refuse_update'")
    triggers = text(
        "SELECT count(*) FROM pg_trigger WHERE tgname IN "
        "('billing_batches_insert_only', 'billing_movements_insert_only')"
    )
    try:
        command.downgrade(cfg, "bila_billing_accounts")
        with engine.connect() as conn:
            names = set(inspect(conn).get_table_names())
            assert not set(LEDGER_TABLES) & names
            assert "billing_audit_log" in names
            assert conn.scalar(guard) == 1
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert set(LEDGER_TABLES) <= set(inspect(conn).get_table_names())
        assert conn.scalar(triggers) == 2
    engine.dispose()
