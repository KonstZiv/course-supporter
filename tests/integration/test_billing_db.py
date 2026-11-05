"""BIL-A on a live database: accounts, deletion, payer, rules, settings, consent.

Each test runs in a transaction that is rolled back (``db_session``). The
deletions are the ones the system has today — a hand-written ``DELETE FROM
tenants`` / ``students``, cascading through the existing foreign keys — and
must neither fail on a billing row nor take one with them.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Table, create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import admin
from course_supporter.billing.accounts import (
    anonymize_student_accounts,
    anonymize_tenant_accounts,
    find_active_account,
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.consent import (
    consent_required,
    record_student_consent,
    student_terms_status,
)
from course_supporter.billing.errors import (
    BillingConflictError,
    BillingInvalidError,
    BillingNotFoundError,
)
from course_supporter.billing.kinds import (
    AccountKind,
    ActionKind,
    Benefit,
    ConsentChannel,
    PayerSource,
    RuleScope,
)
from course_supporter.billing.payer import resolve_payer
from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingAuthorSettings,
    BillingConsent,
    BillingPaymentRule,
    CourseNode,
    Student,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

BILLING_TABLES = (
    "billing_accounts",
    "billing_payment_rules",
    "billing_author_settings",
    "billing_consents",
    "billing_audit_log",
)
ACTOR = "test-admin"
SHA = "a" * 64
V1 = "2026-11-07"
V2 = "2026-12-01"


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    course_id: uuid.UUID
    child_node_id: uuid.UUID
    student_id: uuid.UUID
    other_student_id: uuid.UUID


async def _tenant_with_course(session: AsyncSession) -> World:
    tenant = Tenant(name=f"bil-a-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    course = make_root_course_node(tenant_id=tenant.id, title="Курс", order=0)
    session.add(course)
    await session.flush()
    child = CourseNode(
        tenant_id=tenant.id, parent_id=course.id, title="Розділ", order=0
    )
    students = [
        Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:8]}")
        for _ in range(2)
    ]
    session.add_all([child, *students])
    await session.flush()
    return World(tenant.id, course.id, child.id, students[0].id, students[1].id)


@pytest.fixture()
async def world(db_session: AsyncSession) -> World:
    return await _tenant_with_course(db_session)


async def _audit(
    session: AsyncSession, entity_id: uuid.UUID
) -> list[BillingAuditEntry]:
    rows = await session.execute(
        select(BillingAuditEntry)
        .where(BillingAuditEntry.entity_id == entity_id)
        .order_by(BillingAuditEntry.created_at, BillingAuditEntry.id)
    )
    return list(rows.scalars())


# ── Accounts ───────────────────────────────────────────────────────────


class TestAccounts:
    async def test_created_lazily_once(
        self, db_session: AsyncSession, world: World
    ) -> None:
        assert (
            await find_active_account(
                db_session,
                kind=AccountKind.STUDENT,
                tenant_id=world.tenant_id,
                student_id=world.student_id,
            )
            is None
        )
        first = await get_or_create_student_account(
            db_session, tenant_id=world.tenant_id, student_id=world.student_id
        )
        again = await get_or_create_student_account(
            db_session, tenant_id=world.tenant_id, student_id=world.student_id
        )
        author = await get_or_create_author_account(
            db_session, tenant_id=world.tenant_id
        )
        assert first.id == again.id
        assert (first.kind, first.status) == ("student", "active")
        assert (author.kind, author.student_id) == ("author", None)
        assert author.id != first.id

    async def test_one_active_account_per_owner(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await get_or_create_author_account(db_session, tenant_id=world.tenant_id)
        db_session.add(BillingAccount(kind="author", tenant_id=world.tenant_id))
        with pytest.raises(IntegrityError, match="uq_billing_accounts_active_author"):
            async with db_session.begin_nested():
                await db_session.flush()

    async def test_one_active_account_per_student(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await get_or_create_student_account(
            db_session, tenant_id=world.tenant_id, student_id=world.student_id
        )
        db_session.add(
            BillingAccount(
                kind="student", tenant_id=world.tenant_id, student_id=world.student_id
            )
        )
        with pytest.raises(IntegrityError, match="uq_billing_accounts_active_student"):
            async with db_session.begin_nested():
                await db_session.flush()

    async def test_an_author_account_names_no_student(
        self, db_session: AsyncSession, world: World
    ) -> None:
        db_session.add(
            BillingAccount(
                kind="author", tenant_id=world.tenant_id, student_id=world.student_id
            )
        )
        with pytest.raises(IntegrityError, match="author_has_no_student"):
            async with db_session.begin_nested():
                await db_session.flush()


# ── Deletion and anonymization ─────────────────────────────────────────


async def _everything_billing(session: AsyncSession, world: World) -> BillingAccount:
    """Every kind of billing row for the world; returns the student's account."""
    student = await get_or_create_student_account(
        session, tenant_id=world.tenant_id, student_id=world.student_id
    )
    await record_student_consent(
        session,
        tenant_id=world.tenant_id,
        student_id=world.student_id,
        terms_version=V1,
        current_version=V1,
        text_sha256=SHA,
        channel=ConsentChannel.PORTAL,
    )
    await admin.update_author_settings(
        session, world.tenant_id, actor=ACTOR, benefit=Benefit.UNLIMITED
    )
    await admin.create_payment_rule(
        session,
        actor=ACTOR,
        tenant_id=world.tenant_id,
        scope=RuleScope.STUDENT,
        action_kind=ActionKind.REVIEW,
        student_id=world.student_id,
    )
    await admin.create_payment_rule(
        session,
        actor=ACTOR,
        tenant_id=world.tenant_id,
        scope=RuleScope.COURSE,
        action_kind=ActionKind.REVIEW,
        course_node_id=world.course_id,
    )
    return student


async def _count(session: AsyncSession, table: str) -> int:
    value = await session.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
    return int(value or 0)


class TestDeletion:
    async def test_deleting_the_tenant_keeps_every_billing_row(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await _everything_billing(db_session, world)
        before = {t: await _count(db_session, t) for t in BILLING_TABLES}

        await db_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": world.tenant_id}
        )

        assert await db_session.get(Student, world.student_id) is None  # cascaded
        assert {t: await _count(db_session, t) for t in BILLING_TABLES} == before

    async def test_deleting_the_student_keeps_their_account(
        self, db_session: AsyncSession, world: World
    ) -> None:
        account = await _everything_billing(db_session, world)
        await db_session.execute(
            text("DELETE FROM students WHERE id = :id"), {"id": world.student_id}
        )
        await db_session.refresh(account)  # still there, still active
        assert (account.status, account.student_id) == ("active", world.student_id)

    async def test_anonymizing_a_deleted_student(
        self, db_session: AsyncSession, world: World
    ) -> None:
        account = await _everything_billing(db_session, world)
        other = await get_or_create_student_account(
            db_session, tenant_id=world.tenant_id, student_id=world.other_student_id
        )
        await db_session.execute(
            text("DELETE FROM students WHERE id = :id"), {"id": world.student_id}
        )
        at = datetime(2026, 11, 5, 12, tzinfo=UTC)

        ids = await anonymize_student_accounts(
            db_session, world.student_id, actor=ACTOR, now=at
        )

        assert ids == [account.id]
        await db_session.refresh(account)
        assert (account.status, account.tenant_id, account.student_id) == (
            "closed",
            None,
            None,
        )
        assert account.anonymized_at == at
        assert account.closed_at == at
        await db_session.refresh(other)
        assert other.status == "active"  # another student's account is untouched
        # The consent stays, on the opaque account id.
        consents = await db_session.scalar(
            select(func.count()).where(BillingConsent.account_id == account.id)
        )
        assert consents == 1
        [entry] = await _audit(db_session, account.id)
        assert entry.action == "account.anonymize"
        assert entry.before is not None and entry.after is not None
        assert "student_id" not in entry.before  # the log keeps no owner
        assert entry.after["status"] == "closed"
        # Idempotent.
        assert (
            await anonymize_student_accounts(db_session, world.student_id, actor=ACTOR)
            == []
        )

    async def test_anonymizing_a_deleted_tenant(
        self, db_session: AsyncSession, world: World
    ) -> None:
        student = await _everything_billing(db_session, world)
        author = await get_or_create_author_account(
            db_session, tenant_id=world.tenant_id
        )
        await db_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": world.tenant_id}
        )

        ids = await anonymize_tenant_accounts(db_session, world.tenant_id, actor=ACTOR)

        assert set(ids) == {student.id, author.id}
        rows = await db_session.execute(
            select(BillingAccount).where(BillingAccount.id.in_(ids))
        )
        for account in rows.scalars():
            await db_session.refresh(account)
            assert account.status == "closed"
            assert account.tenant_id is None and account.student_id is None
            assert account.anonymized_at is not None
        # Settings and rules stay, on the opaque account id.
        assert await db_session.get(BillingAuthorSettings, author.id) is not None
        rules = await db_session.scalar(
            select(func.count()).where(BillingPaymentRule.payer_account_id == author.id)
        )
        assert rules == 2

    async def test_a_live_owner_is_not_anonymized(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await _everything_billing(db_session, world)
        with pytest.raises(BillingConflictError, match="not deleted"):
            await anonymize_student_accounts(db_session, world.student_id, actor=ACTOR)
        with pytest.raises(BillingConflictError, match="not deleted"):
            await anonymize_tenant_accounts(db_session, world.tenant_id, actor=ACTOR)

    async def test_a_soft_deleted_student_is_anonymized(
        self, db_session: AsyncSession, world: World
    ) -> None:
        account = await get_or_create_student_account(
            db_session, tenant_id=world.tenant_id, student_id=world.student_id
        )
        student = await db_session.get(Student, world.student_id)
        assert student is not None
        student.deleted_at = datetime.now(UTC)
        await db_session.flush()
        assert await anonymize_student_accounts(
            db_session, world.student_id, actor=ACTOR
        ) == [account.id]


# ── Payer ──────────────────────────────────────────────────────────────


class TestPayer:
    async def _accounts(
        self, session: AsyncSession, world: World
    ) -> tuple[uuid.UUID, uuid.UUID]:
        student = await get_or_create_student_account(
            session, tenant_id=world.tenant_id, student_id=world.student_id
        )
        author = await get_or_create_author_account(session, tenant_id=world.tenant_id)
        return student.id, author.id

    async def _rule(
        self,
        session: AsyncSession,
        world: World,
        scope: RuleScope,
        kind: ActionKind = ActionKind.REVIEW,
    ) -> BillingPaymentRule:
        return await admin.create_payment_rule(
            session,
            actor=ACTOR,
            tenant_id=world.tenant_id,
            scope=scope,
            action_kind=kind,
            course_node_id=world.course_id if scope is RuleScope.COURSE else None,
            student_id=world.student_id if scope is RuleScope.STUDENT else None,
        )

    async def test_default_review_is_paid_by_the_student(
        self, db_session: AsyncSession, world: World
    ) -> None:
        student, _ = await self._accounts(db_session, world)
        got = await resolve_payer(
            db_session,
            action_kind=ActionKind.REVIEW,
            tenant_id=world.tenant_id,
            course_node_id=world.course_id,
            student_id=world.student_id,
        )
        assert (got.payer_account_id, got.beneficiary_account_id) == (student, student)
        assert (got.source, got.rule_id) == (PayerSource.DEFAULT, None)

    @pytest.mark.parametrize(
        "kind", [ActionKind.PROCESSING, ActionKind.TASK_PREPARATION]
    )
    async def test_default_author_work_is_paid_by_the_author(
        self, db_session: AsyncSession, world: World, kind: ActionKind
    ) -> None:
        _, author = await self._accounts(db_session, world)
        got = await resolve_payer(
            db_session,
            action_kind=kind,
            tenant_id=world.tenant_id,
            course_node_id=world.course_id,
            student_id=None,
        )
        assert (got.payer_account_id, got.beneficiary_account_id) == (author, author)
        assert got.source is PayerSource.DEFAULT

    async def test_accounts_are_created_on_the_way(
        self, db_session: AsyncSession, world: World
    ) -> None:
        got = await resolve_payer(
            db_session,
            action_kind=ActionKind.REVIEW,
            tenant_id=world.tenant_id,
            course_node_id=None,
            student_id=world.student_id,
        )
        account = await db_session.get(BillingAccount, got.payer_account_id)
        assert account is not None and account.student_id == world.student_id

    async def test_course_rule(self, db_session: AsyncSession, world: World) -> None:
        student, author = await self._accounts(db_session, world)
        rule = await self._rule(db_session, world, RuleScope.COURSE)
        got = await resolve_payer(
            db_session,
            action_kind=ActionKind.REVIEW,
            tenant_id=world.tenant_id,
            course_node_id=world.course_id,
            student_id=world.student_id,
        )
        assert (got.payer_account_id, got.beneficiary_account_id) == (author, student)
        assert (got.source, got.rule_id) == (PayerSource.COURSE_RULE, rule.id)

    async def test_student_rule_comes_before_course_rule(
        self, db_session: AsyncSession, world: World
    ) -> None:
        await self._accounts(db_session, world)
        await self._rule(db_session, world, RuleScope.COURSE)
        student_rule = await self._rule(db_session, world, RuleScope.STUDENT)
        got = await resolve_payer(
            db_session,
            action_kind=ActionKind.REVIEW,
            tenant_id=world.tenant_id,
            course_node_id=world.course_id,
            student_id=world.student_id,
        )
        assert (got.source, got.rule_id) == (PayerSource.STUDENT_RULE, student_rule.id)

    async def test_student_rule_without_a_course(
        self, db_session: AsyncSession, world: World
    ) -> None:
        _, author = await self._accounts(db_session, world)
        await self._rule(db_session, world, RuleScope.STUDENT)
        got = await resolve_payer(
            db_session,
            action_kind=ActionKind.REVIEW,
            tenant_id=world.tenant_id,
            course_node_id=None,
            student_id=world.student_id,
        )
        assert (got.payer_account_id, got.source) == (author, PayerSource.STUDENT_RULE)

    async def test_an_ended_rule_or_another_kind_does_not_apply(
        self, db_session: AsyncSession, world: World
    ) -> None:
        student, _ = await self._accounts(db_session, world)
        ended = await self._rule(db_session, world, RuleScope.COURSE)
        await admin.end_payment_rule(db_session, ended.id, actor=ACTOR)
        await self._rule(db_session, world, RuleScope.STUDENT, ActionKind.PROCESSING)
        got = await resolve_payer(
            db_session,
            action_kind=ActionKind.REVIEW,
            tenant_id=world.tenant_id,
            course_node_id=world.course_id,
            student_id=world.student_id,
        )
        assert (got.payer_account_id, got.source) == (student, PayerSource.DEFAULT)

    async def test_a_review_needs_a_student(
        self, db_session: AsyncSession, world: World
    ) -> None:
        with pytest.raises(BillingInvalidError):
            await resolve_payer(
                db_session,
                action_kind=ActionKind.REVIEW,
                tenant_id=world.tenant_id,
                course_node_id=world.course_id,
                student_id=None,
            )


# ── Payment rules ──────────────────────────────────────────────────────


class TestRules:
    async def test_a_student_never_pays_for_another_student(
        self, db_session: AsyncSession, world: World
    ) -> None:
        payer = await get_or_create_student_account(
            db_session, tenant_id=world.tenant_id, student_id=world.other_student_id
        )
        with pytest.raises(BillingInvalidError, match="another student"):
            await admin.create_payment_rule(
                db_session,
                actor=ACTOR,
                tenant_id=world.tenant_id,
                scope=RuleScope.STUDENT,
                action_kind=ActionKind.REVIEW,
                student_id=world.student_id,
                payer_account_id=payer.id,
            )

    async def test_the_payer_is_the_author_of_the_same_tenant(
        self, db_session: AsyncSession, world: World
    ) -> None:
        elsewhere = await _tenant_with_course(db_session)
        foreign = await get_or_create_author_account(
            db_session, tenant_id=elsewhere.tenant_id
        )
        with pytest.raises(BillingInvalidError, match="same tenant"):
            await admin.create_payment_rule(
                db_session,
                actor=ACTOR,
                tenant_id=world.tenant_id,
                scope=RuleScope.COURSE,
                action_kind=ActionKind.REVIEW,
                course_node_id=world.course_id,
                payer_account_id=foreign.id,
            )

    async def test_the_target_belongs_to_the_tenant_and_is_a_course(
        self, db_session: AsyncSession, world: World
    ) -> None:
        elsewhere = await _tenant_with_course(db_session)
        for course in (world.child_node_id, elsewhere.course_id):
            with pytest.raises(BillingNotFoundError):
                await admin.create_payment_rule(
                    db_session,
                    actor=ACTOR,
                    tenant_id=world.tenant_id,
                    scope=RuleScope.COURSE,
                    action_kind=ActionKind.REVIEW,
                    course_node_id=course,
                )
        with pytest.raises(BillingNotFoundError):
            await admin.create_payment_rule(
                db_session,
                actor=ACTOR,
                tenant_id=world.tenant_id,
                scope=RuleScope.STUDENT,
                action_kind=ActionKind.REVIEW,
                student_id=elsewhere.student_id,
            )
        with pytest.raises(BillingInvalidError):
            await admin.create_payment_rule(
                db_session,
                actor=ACTOR,
                tenant_id=world.tenant_id,
                scope=RuleScope.COURSE,
                action_kind=ActionKind.REVIEW,
                student_id=world.student_id,
            )

    async def test_one_active_rule_per_target_and_kind(
        self, db_session: AsyncSession, world: World
    ) -> None:
        async def course_rule(kind: ActionKind) -> BillingPaymentRule:
            return await admin.create_payment_rule(
                db_session,
                actor=ACTOR,
                tenant_id=world.tenant_id,
                scope=RuleScope.COURSE,
                action_kind=kind,
                course_node_id=world.course_id,
            )

        rule = await course_rule(ActionKind.REVIEW)
        with pytest.raises(BillingConflictError, match="end it first"):
            await course_rule(ActionKind.REVIEW)
        await course_rule(ActionKind.PROCESSING)
        await admin.end_payment_rule(db_session, rule.id, actor=ACTOR)
        await course_rule(ActionKind.REVIEW)
        listed = await admin.list_payment_rules(db_session, world.tenant_id)
        everything = await admin.list_payment_rules(
            db_session, world.tenant_id, include_ended=True
        )
        assert (len(listed), len(everything)) == (2, 3)

    async def test_create_and_end_are_audited(
        self, db_session: AsyncSession, world: World
    ) -> None:
        rule = await admin.create_payment_rule(
            db_session,
            actor=" kostiantyn ",
            tenant_id=world.tenant_id,
            scope=RuleScope.COURSE,
            action_kind=ActionKind.REVIEW,
            course_node_id=world.course_id,
        )
        ended = await admin.end_payment_rule(db_session, rule.id, actor="olena")
        created, finished = await _audit(db_session, rule.id)
        assert (created.actor, created.action, created.before) == (
            "kostiantyn",
            "payment_rule.create",
            None,
        )
        assert created.after is not None and created.after["active"] is True
        assert created.after["course_node_id"] == str(world.course_id)
        assert (finished.actor, finished.action) == ("olena", "payment_rule.end")
        assert finished.before == created.after
        assert finished.after is not None and finished.after["active"] is False
        assert ended.ended_at is not None and rule.created_by == "kostiantyn"
        with pytest.raises(BillingConflictError, match="already ended"):
            await admin.end_payment_rule(db_session, rule.id, actor=ACTOR)
        with pytest.raises(BillingNotFoundError):
            await admin.end_payment_rule(db_session, uuid.uuid4(), actor=ACTOR)

    async def test_a_change_needs_an_actor(
        self, db_session: AsyncSession, world: World
    ) -> None:
        with pytest.raises(BillingInvalidError, match="actor"):
            await admin.create_payment_rule(
                db_session,
                actor="  ",
                tenant_id=world.tenant_id,
                scope=RuleScope.COURSE,
                action_kind=ActionKind.REVIEW,
                course_node_id=world.course_id,
            )


# ── Author settings ────────────────────────────────────────────────────


class TestAuthorSettings:
    async def test_defaults_without_a_row(
        self, db_session: AsyncSession, world: World
    ) -> None:
        view = await admin.get_author_settings(db_session, world.tenant_id)
        assert (view.benefit, view.monthly_limit_units, view.credit_allowed) == (
            Benefit.NONE,
            None,
            False,
        )
        assert await db_session.get(BillingAuthorSettings, view.account_id) is None

    async def test_monthly_limit_then_unlimited_then_none(
        self, db_session: AsyncSession, world: World
    ) -> None:
        t0 = datetime(2026, 11, 1, tzinfo=UTC)
        t1 = datetime(2026, 11, 2, tzinfo=UTC)
        view = await admin.update_author_settings(
            db_session,
            world.tenant_id,
            actor=ACTOR,
            benefit=Benefit.MONTHLY_LIMIT,
            monthly_limit_units=500,
            now=t0,
        )
        assert (view.benefit, view.monthly_limit_units) == (Benefit.MONTHLY_LIMIT, 500)
        assert view.benefit_started_at == t0
        # A new limit keeps the start; a new kind of benefit restarts it.
        view = await admin.update_author_settings(
            db_session, world.tenant_id, actor=ACTOR, monthly_limit_units=800, now=t1
        )
        assert (view.monthly_limit_units, view.benefit_started_at) == (800, t0)
        view = await admin.update_author_settings(
            db_session, world.tenant_id, actor=ACTOR, benefit=Benefit.UNLIMITED, now=t1
        )
        assert (view.monthly_limit_units, view.benefit_started_at) == (None, t1)
        view = await admin.update_author_settings(
            db_session, world.tenant_id, actor=ACTOR, benefit=Benefit.NONE
        )
        assert view.benefit_started_at is None

        entries = await _audit(db_session, view.account_id)
        assert [e.action for e in entries] == ["author_settings.update"] * 4
        assert entries[0].before is None
        assert entries[0].after is not None
        assert entries[0].after["monthly_limit_units"] == 500
        assert entries[1].before == entries[0].after

    async def test_credit_and_a_no_op(
        self, db_session: AsyncSession, world: World
    ) -> None:
        view = await admin.update_author_settings(
            db_session, world.tenant_id, actor=ACTOR, credit_allowed=False
        )
        assert await _audit(db_session, view.account_id) == []  # nothing changed
        view = await admin.update_author_settings(
            db_session, world.tenant_id, actor=ACTOR, credit_allowed=True
        )
        assert view.credit_allowed is True
        view = await admin.update_author_settings(
            db_session, world.tenant_id, actor=ACTOR, credit_allowed=True
        )
        [entry] = await _audit(db_session, view.account_id)
        assert entry.before is None
        assert entry.after is not None and entry.after["credit_allowed"] is True

    async def test_a_limit_goes_with_monthly_limit_only(
        self, db_session: AsyncSession, world: World
    ) -> None:
        with pytest.raises(BillingInvalidError, match="monthly_limit_units"):
            await admin.update_author_settings(
                db_session, world.tenant_id, actor=ACTOR, monthly_limit_units=10
            )
        with pytest.raises(BillingInvalidError, match="monthly_limit_units > 0"):
            await admin.update_author_settings(
                db_session, world.tenant_id, actor=ACTOR, benefit=Benefit.MONTHLY_LIMIT
            )

    async def test_an_unknown_tenant(self, db_session: AsyncSession) -> None:
        with pytest.raises(BillingNotFoundError):
            await admin.get_author_settings(db_session, uuid.uuid4())

    async def test_read_account(self, db_session: AsyncSession, world: World) -> None:
        await _everything_billing(db_session, world)
        [author] = await admin.find_accounts(db_session, tenant_id=world.tenant_id)
        view = await admin.read_account(db_session, author.id)
        assert view.settings is not None
        assert view.settings.benefit is Benefit.UNLIMITED
        assert len(view.rules_paid) == 2
        [student] = await admin.find_accounts(
            db_session, tenant_id=world.tenant_id, student_id=world.student_id
        )
        student_view = await admin.read_account(db_session, student.id)
        assert student_view.settings is None
        assert [c.terms_version for c in student_view.consents] == [V1]


# ── Consent ────────────────────────────────────────────────────────────


class TestConsent:
    async def _consent(
        self, session: AsyncSession, world: World, version: str, current: str
    ) -> tuple[BillingConsent, bool]:
        return await record_student_consent(
            session,
            tenant_id=world.tenant_id,
            student_id=world.student_id,
            terms_version=version,
            current_version=current,
            text_sha256=SHA,
            channel=ConsentChannel.PORTAL,
        )

    async def test_required_until_given_and_again_for_a_new_revision(
        self, db_session: AsyncSession, world: World
    ) -> None:
        status = await student_terms_status(
            db_session,
            tenant_id=world.tenant_id,
            student_id=world.student_id,
            terms_version=V1,
        )
        assert (status.consent_required, status.consented_at) == (True, None)
        assert (
            await find_active_account(
                db_session,
                kind=AccountKind.STUDENT,
                tenant_id=world.tenant_id,
                student_id=world.student_id,
            )
            is None
        )  # asking creates nothing

        record, created = await self._consent(db_session, world, V1, V1)
        assert created and record.channel == "portal"
        assert not await consent_required(
            db_session, account_id=record.account_id, terms_version=V1
        )
        assert await consent_required(
            db_session, account_id=record.account_id, terms_version=V2
        )

    async def test_the_same_revision_twice_writes_once(
        self, db_session: AsyncSession, world: World
    ) -> None:
        first, created = await self._consent(db_session, world, V1, V1)
        again, created_again = await self._consent(db_session, world, V1, V1)
        assert (created, created_again) == (True, False)
        assert again.id == first.id

    async def test_not_the_revision_in_force(
        self, db_session: AsyncSession, world: World
    ) -> None:
        with pytest.raises(BillingConflictError, match="not the current"):
            await self._consent(db_session, world, V1, V2)

    @pytest.mark.parametrize("table", ["billing_consents", "billing_audit_log"])
    async def test_insert_only(
        self, db_session: AsyncSession, world: World, table: str
    ) -> None:
        await _everything_billing(db_session, world)
        with pytest.raises(DBAPIError, match="insert-only"):
            async with db_session.begin_nested():
                await db_session.execute(
                    text(f"UPDATE {table} SET id = id")  # noqa: S608
                )


# ── Locks: schema ──────────────────────────────────────────────────────


class TestSchemaLocks:
    async def test_no_delete_can_reach_a_billing_row(
        self, db_session: AsyncSession
    ) -> None:
        """Every foreign key of a billing table points inside billing and has
        no ON DELETE action; no table outside billing points at billing. So
        deleting a tenant, a student or a course never reaches a billing row
        — not by one path, not by several (the BIL-D lesson)."""
        rows = (
            await db_session.execute(
                text(
                    "SELECT conrelid::regclass::text, confrelid::regclass::text, "
                    "confdeltype FROM pg_constraint WHERE contype = 'f' AND "
                    "(conrelid::regclass::text LIKE 'billing\\_%' "
                    "OR confrelid::regclass::text LIKE 'billing\\_%')"
                )
            )
        ).all()
        assert rows  # the internal keys exist, so the query looks where it should
        for child, parent, on_delete in rows:
            assert child in BILLING_TABLES, (child, parent)
            assert parent == "billing_accounts", (child, parent)
            assert on_delete == "a", (child, parent, on_delete)  # NO ACTION

    @pytest.mark.parametrize("table", BILLING_TABLES)
    async def test_comments_are_the_orms(
        self, db_session: AsyncSession, table: str
    ) -> None:
        orm: Table = {
            "billing_accounts": BillingAccount,
            "billing_payment_rules": BillingPaymentRule,
            "billing_author_settings": BillingAuthorSettings,
            "billing_consents": BillingConsent,
            "billing_audit_log": BillingAuditEntry,
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


def test_the_migration_goes_down_and_up() -> None:
    """Downgrade drops the five tables and the guard; upgrade brings them back."""
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    guard = text("SELECT count(*) FROM pg_proc WHERE proname = 'billing_refuse_update'")
    try:
        command.downgrade(cfg, "bild_action_cost_estimates")
        with engine.connect() as conn:
            assert not set(BILLING_TABLES) & set(inspect(conn).get_table_names())
            assert conn.scalar(guard) == 0
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert set(BILLING_TABLES) <= set(inspect(conn).get_table_names())
        assert conn.scalar(guard) == 1
    engine.dispose()
