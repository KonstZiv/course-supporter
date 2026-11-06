"""The periodic tasks of billing (BIL-B part 2) on a live database.

Each task: does its work, does nothing more on a second run in the same
minute, and two runs at once write nothing twice. The five-year destruction:
what it deletes, and the locks — an account that was never anonymized, or was
anonymized less than five years ago, is never touched.

The tasks open their own transactions (``session_factory``), so the data here
is committed and wiped after each test. A task runs over the whole database:
assertions are about this test's accounts only.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import pytest
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.billing import ledger
from course_supporter.billing.accounts import (
    anonymize_student_accounts,
    anonymize_tenant_accounts,
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.admin import create_payment_rule
from course_supporter.billing.consent import record_student_consent
from course_supporter.billing.expiry import expire_batches
from course_supporter.billing.journal import add_months
from course_supporter.billing.kinds import (
    ActionKind,
    ConsentChannel,
    PayerSource,
    RuleScope,
)
from course_supporter.billing.periodic import (
    BILLING_ANNUL,
    BILLING_EXPIRE,
    BILLING_PURGE,
    BILLING_STALE_RESERVES,
)
from course_supporter.billing.refunds import refund_hold
from course_supporter.config import Settings, get_settings
from course_supporter.periodic.runner import PeriodicTask, TaskRun, lock_key, run_task
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingAuthorSettings,
    BillingBatch,
    BillingConsent,
    BillingDifference,
    BillingMovement,
    BillingPaymentRule,
    BillingRefund,
    BillingReservation,
    Student,
    Tenant,
)

pytestmark = pytest.mark.requires_db

ACTOR = "test-admin"
SHA = "b" * 64
T = TypeVar("T")

# Every table that holds a billing row of an account, with the column naming it.
ACCOUNT_ROWS = (
    (BillingMovement, BillingMovement.account_id),
    (BillingRefund, BillingRefund.account_id),
    (BillingReservation, BillingReservation.account_id),
    (BillingDifference, BillingDifference.account_id),
    (BillingBatch, BillingBatch.account_id),
    (BillingConsent, BillingConsent.account_id),
    (BillingAuthorSettings, BillingAuthorSettings.account_id),
    (BillingPaymentRule, BillingPaymentRule.payer_account_id),
    (BillingAccount, BillingAccount.id),
)


def key() -> str:
    return str(uuid.uuid4())


# ── The scene: committed data, wiped after the test ────────────────────


@dataclass
class Scene:
    factory: async_sessionmaker[AsyncSession]
    now: datetime
    tenant_ids: list[uuid.UUID] = field(default_factory=list)
    student_ids: list[uuid.UUID] = field(default_factory=list)
    account_ids: list[uuid.UUID] = field(default_factory=list)

    async def do(self, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
        """Run ``fn`` in its own committed transaction."""
        async with self.factory() as session:
            out = await fn(session)
            await session.commit()
        return out

    async def tenant(self) -> uuid.UUID:
        async def make(s: AsyncSession) -> uuid.UUID:
            tenant = Tenant(name=f"bil-b2-{uuid.uuid4().hex[:8]}")
            s.add(tenant)
            await s.flush()
            return tenant.id

        tenant_id = await self.do(make)
        self.tenant_ids.append(tenant_id)
        return tenant_id

    async def author(self, tenant_id: uuid.UUID) -> uuid.UUID:
        async def make(s: AsyncSession) -> uuid.UUID:
            return (await get_or_create_author_account(s, tenant_id=tenant_id)).id

        account_id = await self.do(make)
        self.account_ids.append(account_id)
        return account_id

    async def student(self, tenant_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
        """A student and their billing account."""

        async def make(s: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
            student = Student(tenant_id=tenant_id, external_id=f"s-{key()[:8]}")
            s.add(student)
            await s.flush()
            account = await get_or_create_student_account(
                s, tenant_id=tenant_id, student_id=student.id
            )
            return student.id, account.id

        student_id, account_id = await self.do(make)
        self.student_ids.append(student_id)
        self.account_ids.append(account_id)
        return student_id, account_id

    async def buy(self, account_id: uuid.UUID, units: int, at: datetime) -> uuid.UUID:
        async def make(s: AsyncSession) -> uuid.UUID:
            done = await ledger.credit_purchase(
                s,
                account_id=account_id,
                units=units,
                amount_paid_minor=1000,
                currency="USD",
                now=at,
            )
            return done.batch.id

        return await self.do(make)

    async def reserve(self, account_id: uuid.UUID, units: int, at: datetime) -> str:
        action_key = key()

        async def make(s: AsyncSession) -> None:
            await ledger.reserve(
                s,
                action_kind=ActionKind.REVIEW,
                action_key=action_key,
                account_id=account_id,
                units=units,
                now=at,
            )

        await self.do(make)
        return action_key

    async def settle(
        self,
        payer: uuid.UUID,
        fact: int,
        at: datetime,
        *,
        beneficiary: uuid.UUID | None = None,
        source: PayerSource = PayerSource.DEFAULT,
        rule_id: uuid.UUID | None = None,
    ) -> None:
        async def make(s: AsyncSession) -> None:
            await ledger.settle(
                s,
                action_kind=ActionKind.REVIEW,
                action_key=key(),
                fact_units=fact,
                beneficiary_account_id=beneficiary or payer,
                payer_source=source,
                rule_id=rule_id,
                account_id=payer,
                now=at,
            )

        await self.do(make)

    async def delete_student_and_anonymize(
        self, student_id: uuid.UUID, at: datetime
    ) -> None:
        async def make(s: AsyncSession) -> None:
            await s.execute(
                text("DELETE FROM students WHERE id = :id"), {"id": student_id}
            )
            await anonymize_student_accounts(s, student_id, actor=ACTOR, now=at)

        await self.do(make)

    async def delete_tenant_and_anonymize(
        self, tenant_id: uuid.UUID, at: datetime
    ) -> None:
        async def make(s: AsyncSession) -> None:
            await s.execute(
                text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
            )
            await anonymize_tenant_accounts(s, tenant_id, actor=ACTOR, now=at)

        await self.do(make)

    async def rows(self, account_id: uuid.UUID) -> dict[str, int]:
        """The account's rows per table, its audit entries included."""

        async def count(s: AsyncSession) -> dict[str, int]:
            out = {}
            for model, column in ACCOUNT_ROWS:
                out[model.__tablename__] = int(
                    await s.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(column == account_id)
                    )
                    or 0
                )
            out["billing_audit_log"] = int(
                await s.scalar(
                    select(func.count())
                    .select_from(BillingAuditEntry)
                    .where(BillingAuditEntry.entity_id == account_id)
                )
                or 0
            )
            return out

        return await self.do(count)

    async def journal(self, account_id: uuid.UUID) -> list[tuple[str, int]]:
        async def read(s: AsyncSession) -> list[tuple[str, int]]:
            return [(m.kind, m.units) for m in await ledger.statement(s, account_id)]

        return await self.do(read)

    async def wipe(self) -> None:
        async def clean(s: AsyncSession) -> None:
            ids = self.account_ids
            batch_ids = list(
                (
                    await s.execute(
                        select(BillingBatch.id).where(BillingBatch.account_id.in_(ids))
                    )
                ).scalars()
            )
            rule_ids = list(
                (
                    await s.execute(
                        select(BillingPaymentRule.id).where(
                            or_(
                                BillingPaymentRule.payer_account_id.in_(ids),
                                BillingPaymentRule.tenant_id.in_(self.tenant_ids),
                            )
                        )
                    )
                ).scalars()
            )
            await s.execute(
                delete(BillingMovement).where(
                    or_(
                        BillingMovement.account_id.in_(ids),
                        BillingMovement.beneficiary_account_id.in_(ids),
                    )
                )
            )
            for model, column in ACCOUNT_ROWS:
                if model is BillingPaymentRule:
                    column, ids_of = BillingPaymentRule.id, rule_ids
                else:
                    ids_of = ids
                await s.execute(delete(model).where(column.in_(ids_of)))
            await s.execute(
                delete(BillingAuditEntry).where(
                    BillingAuditEntry.entity_id.in_([*ids, *batch_ids, *rule_ids])
                )
            )
            await s.execute(delete(Student).where(Student.id.in_(self.student_ids)))
            await s.execute(delete(Tenant).where(Tenant.id.in_(self.tenant_ids)))

        await self.do(clean)


@pytest.fixture()
async def scene(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[Scene]:
    made = Scene(session_factory, now=datetime.now(UTC))
    yield made
    await made.wipe()


async def run(
    scene: Scene,
    task: PeriodicTask,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    **settings: Any,
) -> TaskRun:
    configured: Settings = get_settings().model_copy(update=settings)
    done = await run_task(
        task,
        scene.factory,
        settings=configured,
        now=now or scene.now,
        dry_run=dry_run,
    )
    assert done.error is None, done.error
    return done


async def run_twice_at_once(scene: Scene, task: PeriodicTask) -> list[TaskRun]:
    runs = await asyncio.gather(run(scene, task), run(scene, task))
    # One took the lock; the other was skipped, or came after and found nothing.
    assert all(r.skipped in (None, "busy") for r in runs)
    return list(runs)


def months_ago(scene: Scene, months: int) -> datetime:
    return add_months(scene.now, -months)


# ── billing_expire ─────────────────────────────────────────────────────


class TestExpire:
    async def test_the_free_units_of_an_expired_batch_burn(self, scene: Scene) -> None:
        tenant = await scene.tenant()
        author = await scene.author(tenant)
        await scene.buy(author, 100, at=months_ago(scene, 7))
        await scene.buy(author, 40, at=months_ago(scene, 1))  # still valid

        done = await run(scene, BILLING_EXPIRE)

        assert done.processed >= 1
        assert await scene.journal(author) == [
            ("credit", 100),
            ("credit", 40),
            ("batch_expired", 100),
        ]

    async def test_a_second_run_in_the_same_minute_changes_nothing(
        self, scene: Scene
    ) -> None:
        tenant = await scene.tenant()
        author = await scene.author(tenant)
        await scene.buy(author, 100, at=months_ago(scene, 7))
        await run(scene, BILLING_EXPIRE)
        before = await scene.journal(author)

        again = await run(scene, BILLING_EXPIRE)

        assert again.processed == 0
        assert await scene.journal(author) == before

    async def test_two_runs_at_once_burn_once(self, scene: Scene) -> None:
        tenant = await scene.tenant()
        author = await scene.author(tenant)
        await scene.buy(author, 100, at=months_ago(scene, 7))

        await run_twice_at_once(scene, BILLING_EXPIRE)

        assert (await scene.journal(author)).count(("batch_expired", 100)) == 1

    async def test_a_run_finding_the_lock_held_is_skipped(self, scene: Scene) -> None:
        tenant = await scene.tenant()
        author = await scene.author(tenant)
        await scene.buy(author, 100, at=months_ago(scene, 7))

        async with scene.factory() as holder:
            await holder.execute(
                select(func.pg_advisory_xact_lock(lock_key(BILLING_EXPIRE.name)))
            )
            done = await run(scene, BILLING_EXPIRE)
            await holder.rollback()

        assert (done.skipped, done.processed) == ("busy", 0)
        assert ("batch_expired", 100) not in await scene.journal(author)

    async def test_without_the_lock_the_account_lock_still_burns_once(
        self, scene: Scene
    ) -> None:
        """The advisory lock spares work; correctness is the account's lock:
        a second burn waits for the first and then finds nothing free."""
        tenant = await scene.tenant()
        author = await scene.author(tenant)
        await scene.buy(author, 100, at=months_ago(scene, 7))

        async with scene.factory() as a, scene.factory() as b:
            first = await expire_batches(a, scene.now)
            second = asyncio.create_task(expire_batches(b, scene.now))
            await asyncio.sleep(0.5)
            assert not second.done()  # B waits on A's lock of the account
            await a.commit()
            late = await asyncio.wait_for(second, timeout=10)
            await b.commit()

        assert [x.units for x in first if x.account_id == author] == [100]
        assert not [x for x in late if x.account_id == author]


# ── billing_annul ──────────────────────────────────────────────────────


class TestAnnul:
    async def test_a_difference_past_its_term_is_annulled(self, scene: Scene) -> None:
        tenant = await scene.tenant()
        _, student = await scene.student(tenant)
        await scene.settle(student, 30, at=months_ago(scene, 7))  # nothing to pay with
        await scene.settle(student, 5, at=months_ago(scene, 1))  # not yet due

        done = await run(scene, BILLING_ANNUL)

        assert done.processed >= 1
        journal = await scene.journal(student)
        assert journal.count(("difference_annulled", 30)) == 1
        assert ("difference_annulled", 5) not in journal

    async def test_a_second_run_changes_nothing(self, scene: Scene) -> None:
        tenant = await scene.tenant()
        _, student = await scene.student(tenant)
        await scene.settle(student, 30, at=months_ago(scene, 7))
        await run(scene, BILLING_ANNUL)
        before = await scene.journal(student)

        again = await run(scene, BILLING_ANNUL)

        assert again.processed == 0
        assert await scene.journal(student) == before

    async def test_two_runs_at_once_annul_once(self, scene: Scene) -> None:
        tenant = await scene.tenant()
        _, student = await scene.student(tenant)
        await scene.settle(student, 30, at=months_ago(scene, 7))

        await run_twice_at_once(scene, BILLING_ANNUL)

        assert (await scene.journal(student)).count(("difference_annulled", 30)) == 1


# ── billing_stale_reserves ─────────────────────────────────────────────


class TestStaleReserves:
    async def _scene(self, scene: Scene) -> tuple[uuid.UUID, str, str]:
        tenant = await scene.tenant()
        author = await scene.author(tenant)
        await scene.buy(author, 100, at=scene.now - timedelta(days=2))
        hanging = await scene.reserve(author, 40, at=scene.now - timedelta(hours=25))
        running = await scene.reserve(author, 10, at=scene.now - timedelta(hours=1))
        return author, hanging, running

    async def _states(self, scene: Scene, *keys: str) -> list[str]:
        async def read(s: AsyncSession) -> list[str]:
            rows = await s.execute(
                select(BillingReservation.action_key, BillingReservation.state).where(
                    BillingReservation.action_key.in_(keys)
                )
            )
            states = dict(rows.tuples().all())
            return [states[k] for k in keys]

        return await scene.do(read)

    async def test_a_reserve_older_than_the_threshold_is_released_with_a_warning(
        self, scene: Scene
    ) -> None:
        author, hanging, running = await self._scene(scene)

        with capture_logs() as logs:
            done = await run(scene, BILLING_STALE_RESERVES)

        assert done.processed >= 1
        assert await self._states(scene, hanging, running) == ["released", "active"]
        released = await scene.do(
            lambda s: s.execute(
                select(BillingMovement.units, BillingMovement.action_key).where(
                    BillingMovement.account_id == author,
                    BillingMovement.kind == "reserve_release",
                )
            )
        )
        assert [tuple(r) for r in released.all()] == [(40, hanging)]  # its own key
        warnings = [
            e
            for e in logs
            if e["event"] == "billing_stale_reserve_released"
            and e["action_key"] == hanging
        ]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["released_units"] == 40
        assert warnings[0]["threshold_hours"] == 24

    async def test_the_threshold_is_a_setting(self, scene: Scene) -> None:
        _, hanging, running = await self._scene(scene)

        await run(scene, BILLING_STALE_RESERVES, billing_stale_reserve_hours=48)

        assert await self._states(scene, hanging, running) == ["active", "active"]

    async def test_a_second_run_changes_nothing(self, scene: Scene) -> None:
        author, _, _ = await self._scene(scene)
        await run(scene, BILLING_STALE_RESERVES)
        before = await scene.journal(author)

        again = await run(scene, BILLING_STALE_RESERVES)

        assert again.processed == 0
        assert await scene.journal(author) == before

    async def test_two_runs_at_once_release_once(self, scene: Scene) -> None:
        author, _, _ = await self._scene(scene)

        await run_twice_at_once(scene, BILLING_STALE_RESERVES)

        assert (await scene.journal(author)).count(("reserve_release", 40)) == 1

    async def test_a_reserve_settled_meanwhile_is_left_alone(
        self, scene: Scene
    ) -> None:
        author, hanging, _ = await self._scene(scene)

        async def finish(s: AsyncSession) -> None:
            await ledger.settle(
                s,
                action_kind=ActionKind.REVIEW,
                action_key=hanging,
                fact_units=25,
                beneficiary_account_id=author,
                payer_source=PayerSource.DEFAULT,
                now=scene.now,
            )

        await scene.do(finish)
        await run(scene, BILLING_STALE_RESERVES)

        assert await self._states(scene, hanging) == ["settled"]


# ── billing_purge ──────────────────────────────────────────────────────

ALL_GONE = {
    "billing_movements": 0,
    "billing_refunds": 0,
    "billing_reservations": 0,
    "billing_differences": 0,
    "billing_batches": 0,
    "billing_consents": 0,
    "billing_author_settings": 0,
    "billing_payment_rules": 0,
    "billing_accounts": 0,
    "billing_audit_log": 0,
}


class TestPurge:
    async def _full_student(
        self, scene: Scene, *, anonymized_years_ago: int, months: int = 2
    ) -> uuid.UUID:
        """A student account with every kind of billing row, anonymized
        ``anonymized_years_ago`` years (plus ``months`` months) ago."""
        tenant = await scene.tenant()
        student_id, account = await scene.student(tenant)
        at = add_months(scene.now, -12 * anonymized_years_ago - months)
        before = at - timedelta(days=30)
        batch = await scene.buy(account, 100, at=before)
        await scene.settle(account, 20, at=before)  # a debit
        await scene.reserve(account, 10, at=before)  # a reservation
        await scene.do(
            lambda s: refund_hold(
                s,
                account_id=account,
                batch_id=batch,
                units=5,
                paddle_adjustment_id=f"adj_{key()}",
                now=before,
            )
        )
        await scene.do(
            lambda s: record_student_consent(
                s,
                tenant_id=tenant,
                student_id=student_id,
                terms_version="2026-11-09",
                current_version="2026-11-09",
                text_sha256=SHA,
                channel=ConsentChannel.PORTAL,
            )
        )
        # 65 free units left: a fact of 200 takes them and owes 135.
        await scene.settle(account, 200, at=before)  # a difference
        await scene.delete_student_and_anonymize(student_id, at)
        return account

    async def test_an_account_anonymized_five_years_ago_is_destroyed(
        self, scene: Scene
    ) -> None:
        account = await self._full_student(scene, anonymized_years_ago=5)
        rows = await scene.rows(account)
        for table in (
            "billing_movements",
            "billing_refunds",
            "billing_reservations",
            "billing_differences",
            "billing_batches",
            "billing_consents",
        ):
            assert rows[table], table  # the scene holds every kind of row
        assert rows["billing_audit_log"] == 1  # account.anonymize

        done = await run(scene, BILLING_PURGE)

        assert done.processed >= sum(rows.values())
        assert done.details["accounts"] >= 1
        assert await scene.rows(account) == ALL_GONE

    async def test_a_difference_is_destroyed_with_its_account(
        self, scene: Scene
    ) -> None:
        tenant = await scene.tenant()
        student_id, account = await scene.student(tenant)
        at = add_months(scene.now, -12 * 6)
        await scene.settle(account, 30, at=at - timedelta(days=1))
        await scene.delete_student_and_anonymize(student_id, at)
        assert (await scene.rows(account))["billing_differences"] == 1

        await run(scene, BILLING_PURGE)

        assert await scene.rows(account) == ALL_GONE

    async def test_never_an_active_account_nor_one_anonymized_recently(
        self, scene: Scene
    ) -> None:
        """Lock: only ``closed AND anonymized_at <= now - 5 years`` qualifies."""
        recent = await self._full_student(
            scene, anonymized_years_ago=4, months=11
        )  # 4 years 11 months ago
        tenant = await scene.tenant()
        active = await scene.author(tenant)
        await scene.buy(active, 100, at=add_months(scene.now, -12 * 10))
        closed_not_anonymized = await self._closed_not_anonymized(scene)
        before = {
            a: await scene.rows(a) for a in (recent, active, closed_not_anonymized)
        }

        await run(scene, BILLING_PURGE)

        assert {
            a: await scene.rows(a) for a in (recent, active, closed_not_anonymized)
        } == before

    async def test_never_an_account_never_anonymized_whatever_the_date(
        self, scene: Scene
    ) -> None:
        """Lock: a hundred years on, an account never anonymized is still
        there — active, or closed without anonymization."""
        tenant = await scene.tenant()
        active = await scene.author(tenant)
        await scene.buy(active, 100, at=add_months(scene.now, -12 * 10))
        closed_not_anonymized = await self._closed_not_anonymized(scene)
        before = {a: await scene.rows(a) for a in (active, closed_not_anonymized)}

        await run(scene, BILLING_PURGE, now=add_months(scene.now, 12 * 100))

        assert {a: await scene.rows(a) for a in (active, closed_not_anonymized)} == (
            before
        )

    async def _closed_not_anonymized(self, scene: Scene) -> uuid.UUID:
        tenant = await scene.tenant()

        async def make(s: AsyncSession) -> uuid.UUID:
            account = BillingAccount(
                kind="author",
                tenant_id=tenant,
                status="closed",
                created_at=add_months(scene.now, -12 * 12),
                closed_at=add_months(scene.now, -12 * 11),
            )
            s.add(account)
            await s.flush()
            return account.id

        account_id = await scene.do(make)
        scene.account_ids.append(account_id)
        return account_id

    async def test_the_retention_is_a_setting(self, scene: Scene) -> None:
        account = await self._full_student(scene, anonymized_years_ago=5)
        before = await scene.rows(account)

        await run(scene, BILLING_PURGE, billing_retention_years=6)

        assert await scene.rows(account) == before

    async def test_a_dry_run_counts_and_deletes_nothing(self, scene: Scene) -> None:
        account = await self._full_student(scene, anonymized_years_ago=5)
        before = await scene.rows(account)

        done = await run(scene, BILLING_PURGE, dry_run=True)

        assert done.processed >= sum(before.values())
        assert await scene.rows(account) == before

    async def test_a_second_run_finds_nothing(self, scene: Scene) -> None:
        await self._full_student(scene, anonymized_years_ago=5)
        await run(scene, BILLING_PURGE)

        again = await run(scene, BILLING_PURGE)

        assert again.processed == 0

    async def test_two_runs_at_once_destroy_once(self, scene: Scene) -> None:
        first = await self._full_student(scene, anonymized_years_ago=5)
        second = await self._full_student(scene, anonymized_years_ago=6)

        await run_twice_at_once(scene, BILLING_PURGE)

        assert await scene.rows(first) == ALL_GONE
        assert await scene.rows(second) == ALL_GONE

    async def test_in_batches_of_the_setting(self, scene: Scene) -> None:
        accounts = [
            await self._full_student(scene, anonymized_years_ago=5) for _ in range(3)
        ]

        done = await run(scene, BILLING_PURGE, billing_purge_batch_size=1)

        assert done.details["accounts"] >= 3
        for account in accounts:
            assert await scene.rows(account) == ALL_GONE

    async def _paid_by_the_author(
        self, scene: Scene, *, student_first: bool
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        """An author pays for a student's review by a student rule: the
        author's movement names the student's account as the beneficiary."""
        tenant = await scene.tenant()
        if student_first:
            student_id, student = await scene.student(tenant)
            author = await scene.author(tenant)
        else:
            author = await scene.author(tenant)
            student_id, student = await scene.student(tenant)
        at = add_months(scene.now, -12 * 6)
        await scene.buy(author, 100, at=at - timedelta(days=30))

        async def rule(s: AsyncSession) -> uuid.UUID:
            made = await create_payment_rule(
                s,
                actor=ACTOR,
                tenant_id=tenant,
                scope=RuleScope.STUDENT,
                action_kind=ActionKind.REVIEW,
                student_id=student_id,
            )
            return made.id

        rule_id = await scene.do(rule)
        await scene.settle(
            author,
            20,
            at=at - timedelta(days=10),
            beneficiary=student,
            source=PayerSource.STUDENT_RULE,
            rule_id=rule_id,
        )
        # The student's own rows: a batch, its credit movement, a consent.
        await scene.buy(student, 50, at=at - timedelta(days=20))
        await scene.do(
            lambda s: record_student_consent(
                s,
                tenant_id=tenant,
                student_id=student_id,
                terms_version="2026-11-09",
                current_version="2026-11-09",
                text_sha256=SHA,
                channel=ConsentChannel.PORTAL,
            )
        )
        await scene.delete_student_and_anonymize(student_id, at)
        return tenant, author, student, rule_id

    async def test_a_row_another_account_names_stays_alone_until_that_one_goes(
        self, scene: Scene
    ) -> None:
        """Operator 2026-11-06, option b. The author (active) paid for the
        student's review: the author's movement names the student's account.
        Everything of the student goes but the account row, which stays —
        anonymized — while the author's journal points at it, counted in the
        INFO line, never as a WARNING. Once the author is destroyed, the next
        run deletes the row."""
        tenant, author, student, _ = await self._paid_by_the_author(
            scene, student_first=True
        )
        own = await scene.rows(student)
        assert own["billing_batches"] and own["billing_consents"]
        author_rows = await scene.rows(author)
        author_journal = await scene.journal(author)

        with capture_logs() as logs:
            done = await run(scene, BILLING_PURGE)

        assert await scene.rows(student) == {**ALL_GONE, "billing_accounts": 1}
        stub = await scene.do(lambda s: s.get(BillingAccount, student))
        assert stub is not None
        assert (stub.tenant_id, stub.student_id, stub.status) == (None, None, "closed")
        assert await scene.rows(author) == author_rows  # the author is untouched
        assert await scene.journal(author) == author_journal
        assert done.details["kept_as_reference"] >= 1
        [line] = [e for e in logs if e["event"] == "periodic_task_run"]
        assert line["log_level"] == "info"
        assert line["kept_as_reference"] == done.details["kept_as_reference"]
        assert not [e for e in logs if e["log_level"] == "warning"]

        # The next night: still a reference, counted again, nothing else.
        again = await run(scene, BILLING_PURGE)
        assert again.details["kept_as_reference"] >= 1
        assert await scene.rows(student) == {**ALL_GONE, "billing_accounts": 1}

        # The author goes. One account per batch: the student's batch comes
        # first (its id is lower), while the author's movement still exists.
        await scene.delete_tenant_and_anonymize(tenant, add_months(scene.now, -12 * 6))
        assert student < author
        await run(scene, BILLING_PURGE, billing_purge_batch_size=1)
        assert await scene.rows(author) == ALL_GONE
        assert await scene.rows(student) == {**ALL_GONE, "billing_accounts": 1}

        # The next run deletes the row nothing points at any more.
        await run(scene, BILLING_PURGE)
        assert await scene.rows(student) == ALL_GONE

    async def test_both_due_both_destroyed_whatever_the_order(
        self, scene: Scene
    ) -> None:
        """Once the author is due too, both go in one run: the student's account
        row, kept at first (it comes first in id order), goes in the second pass
        over the batch."""
        tenant, author, student, rule_id = await self._paid_by_the_author(
            scene, student_first=True
        )
        assert student < author  # the student comes first: its row is kept at first
        await scene.delete_tenant_and_anonymize(tenant, add_months(scene.now, -12 * 6))

        done = await run(scene, BILLING_PURGE)

        assert done.details["accounts"] >= 2
        assert await scene.rows(author) == ALL_GONE
        assert await scene.rows(student) == ALL_GONE
        rule_audit = await scene.do(
            lambda s: s.scalar(
                select(func.count())
                .select_from(BillingAuditEntry)
                .where(BillingAuditEntry.entity_id == rule_id)
            )
        )
        assert rule_audit == 0  # payment_rule.create and .end went with the rule
