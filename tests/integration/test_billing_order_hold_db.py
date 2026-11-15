"""BIL-E part D on a live database: a refund hold keyed by the provider's order.

The administrator holds units of a purchase by its order
(``billing_admin.py refund-hold``), refunds the money in Creem's dashboard,
and Creem's ``refund.created`` (``refunds.refund_at_provider``, as the webhook
calls it) completes the hold. A batch of 500 units paid €5.00 throughout.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.billing import admin, ledger, refunds
from course_supporter.billing.accounts import get_or_create_student_account
from course_supporter.billing.errors import BillingConflictError, BillingNotFoundError
from course_supporter.billing.kinds import (
    ActionKind,
    AuditAction,
    MovementKind,
    RefundState,
)
from course_supporter.storage.orm import (
    BillingAuditEntry,
    BillingBatch,
    BillingMovement,
    BillingRefund,
    BillingReservation,
    Student,
    Tenant,
)

pytestmark = pytest.mark.requires_db

ACTOR = "test-admin"
PROVIDER = "creem"


@pytest.fixture()
async def purchase(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[uuid.UUID, str]]:
    """A student's account with one Creem purchase: 500 units for €5.00."""
    order = f"ord_{uuid.uuid4().hex}"
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-e-d-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        student = Student(tenant_id=tenant.id, external_id="s")
        session.add(student)
        await session.flush()
        account = await get_or_create_student_account(
            session, tenant_id=tenant.id, student_id=student.id
        )
        await ledger.credit_purchase(
            session,
            account_id=account.id,
            units=500,
            amount_paid_minor=500,
            currency="EUR",
            provider=PROVIDER,
            provider_transaction_id=order,
            provider_line_id=order,
        )
        await session.commit()
    yield account.id, order
    # The holds by order go: the migration tests of other modules walk the
    # schema down, which refuses while one exists.
    async with session_factory() as session:
        refund_ids = select(BillingRefund.id).where(
            BillingRefund.account_id == account.id
        )
        await session.execute(
            delete(BillingAuditEntry).where(BillingAuditEntry.entity_id.in_(refund_ids))
        )
        for table in (BillingMovement, BillingRefund, BillingBatch):
            await session.execute(delete(table).where(table.account_id == account.id))
        await session.commit()


async def hold(
    session_factory: async_sessionmaker[AsyncSession], order: str, units: int
) -> BillingRefund:
    async with session_factory() as session:
        refund, _ = await admin.hold_refund_by_order(
            session,
            provider=PROVIDER,
            order_id=order,
            units=units,
            reason="the user asked",
            actor=ACTOR,
        )
        await session.commit()
        return refund


async def refund_created(
    session_factory: async_sessionmaker[AsyncSession],
    order: str,
    amount_minor: int,
    adjustment: str = "ref_1",
) -> refunds.Taken:
    async with session_factory() as session:
        taken = await refunds.refund_at_provider(
            session,
            provider=PROVIDER,
            provider_adjustment_id=f"{adjustment}-{order}",
            transaction_id=order,
            amount_minor=amount_minor,
            currency="EUR",
        )
        await session.commit()
        return taken


async def balance(
    session_factory: async_sessionmaker[AsyncSession], account: uuid.UUID
) -> ledger.Balance:
    async with session_factory() as session:
        return await ledger.balance(session, account)


async def completed(
    session_factory: async_sessionmaker[AsyncSession], account: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.coalesce(func.sum(BillingMovement.units), 0)).where(
                    BillingMovement.account_id == account,
                    BillingMovement.kind == MovementKind.REFUND_COMPLETED.value,
                )
            )
            or 0
        )


class TestHoldByOrder:
    async def test_hold_250_then_refund_of_2_50_takes_250_once(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        purchase: tuple[uuid.UUID, str],
    ) -> None:
        account, order = purchase
        await hold(session_factory, order, 250)
        held = await balance(session_factory, account)
        assert (held.held, held.available) == (250, 250)

        taken = await refund_created(session_factory, order, 250)
        again = await refund_created(session_factory, order, 250)

        assert (taken.units, again.units, again.repeated) == (250, 250, True)
        assert await completed(session_factory, account) == 250
        after = await balance(session_factory, account)
        assert (after.balance, after.held, after.available) == (250, 0, 250)
        assert taken.refund is not None
        assert taken.refund.state == RefundState.APPROVED.value
        assert taken.refund.provider_adjustment_id == f"ref_1-{order}"

    async def test_hold_300_refund_of_250_units_frees_50(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        purchase: tuple[uuid.UUID, str],
    ) -> None:
        account, order = purchase
        await hold(session_factory, order, 300)

        taken = await refund_created(session_factory, order, 250)

        assert taken.units == 250
        assert await completed(session_factory, account) == 250
        after = await balance(session_factory, account)
        assert (after.balance, after.held, after.available) == (250, 0, 250)

    async def test_a_refund_beyond_the_hold_takes_no_more_than_held(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        purchase: tuple[uuid.UUID, str],
    ) -> None:
        account, order = purchase
        await hold(session_factory, order, 100)

        taken = await refund_created(session_factory, order, 250)

        assert (taken.wanted, taken.units) == (250, 100)
        assert (await balance(session_factory, account)).balance == 400

    async def test_without_a_hold_as_before(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        purchase: tuple[uuid.UUID, str],
    ) -> None:
        account, order = purchase

        taken = await refund_created(session_factory, order, 250)

        assert taken.units == 250
        assert (await balance(session_factory, account)).balance == 250

    async def test_release_when_the_refund_did_not_happen(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        purchase: tuple[uuid.UUID, str],
    ) -> None:
        account, order = purchase
        held = await hold(session_factory, order, 250)

        async with session_factory() as session:
            released = await admin.release_refund_by_order(
                session, provider=PROVIDER, order_id=order, actor=ACTOR
            )
            await session.commit()
            entries = list(
                (
                    await session.execute(
                        select(BillingAuditEntry.action)
                        .where(BillingAuditEntry.entity_id == held.id)
                        .order_by(BillingAuditEntry.created_at)
                    )
                ).scalars()
            )

        assert released.state == RefundState.REJECTED.value
        assert (await balance(session_factory, account)).available == 500
        assert entries == [
            AuditAction.REFUND_ORDER_HOLD.value,
            AuditAction.REFUND_ORDER_RELEASE.value,
        ]
        async with session_factory() as session:
            with pytest.raises(BillingNotFoundError):
                await admin.release_refund_by_order(
                    session, provider=PROVIDER, order_id=order, actor=ACTOR
                )

    async def test_the_same_hold_again_changes_nothing_another_is_refused(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        purchase: tuple[uuid.UUID, str],
    ) -> None:
        account, order = purchase
        first = await hold(session_factory, order, 250)
        second = await hold(session_factory, order, 250)

        assert first.id == second.id
        async with session_factory() as session:
            with pytest.raises(BillingConflictError):
                await admin.hold_refund_by_order(
                    session,
                    provider=PROVIDER,
                    order_id=order,
                    units=100,
                    reason="r",
                    actor=ACTOR,
                )
        assert (await balance(session_factory, account)).held == 250

    async def test_only_free_units_are_held(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        purchase: tuple[uuid.UUID, str],
    ) -> None:
        account, order = purchase
        async with session_factory() as session:
            await ledger.reserve(
                session,
                action_kind=ActionKind.REVIEW,
                action_key=str(uuid.uuid4()),
                account_id=account,
                units=200,
            )
            await session.commit()
        async with session_factory() as session:
            with pytest.raises(BillingConflictError):
                await admin.hold_refund_by_order(
                    session,
                    provider=PROVIDER,
                    order_id=order,
                    units=301,
                    reason="r",
                    actor=ACTOR,
                )
        async with session_factory() as session:
            reservation = (
                await session.execute(
                    select(BillingReservation).where(
                        BillingReservation.account_id == account
                    )
                )
            ).scalar_one()
            await ledger.release(
                session,
                action_kind=ActionKind.REVIEW,
                action_key=reservation.action_key,
            )
            await session.commit()


def test_the_script_has_both_commands() -> None:
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "billing_admin_script", Path("scripts/billing_admin.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["billing_admin_script"] = module
    spec.loader.exec_module(module)
    parser = module.build_parser()

    held = parser.parse_args(
        [
            "refund-hold",
            "--order",
            "ord_1",
            "--units",
            "250",
            "--reason",
            "asked",
            "--actor",
            "a",
        ]
    )
    released = parser.parse_args(["refund-release", "--order", "ord_1", "--actor", "a"])

    assert (held.provider, held.units, held.order) == ("creem", 250, "ord_1")
    assert released.provider == "creem"
