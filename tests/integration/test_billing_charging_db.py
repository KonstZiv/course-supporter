"""BIL-E on a live database: every paid action reserves, is charged its fact, releases.

The rules are in ``billing/charging.py``; the wiring in ``billing/funds.py``.
Numbers: K = 1 700 units a dollar and a margin of 0.20
(``config/cost_estimate.yaml``), E = $0.05 → P = ceil(0.05 x 1.2 x 1700) = 102,
2E = 170 units; a call of $0.04 is F = 68 units.

The port and the gates open sessions of their own and commit, so each test
builds a world of its own (a fresh tenant) and ends every reserve it makes.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.billing import admin, benefit, charging, funds, ledger
from course_supporter.billing.accounts import (
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.kinds import (
    ActionKind,
    Benefit,
    MovementKind,
    PayerSource,
    RuleScope,
)
from course_supporter.billing.stale_reserves import release_stale_reservations
from course_supporter.call_outcome import FundsDecision
from course_supporter.cost_estimate.actions import MaterialFactors
from course_supporter.cost_kinds import CostKind
from course_supporter.funds_port import (
    AlwaysEnoughFundsPort,
    SubmissionContext,
    SubmissionOutcome,
    VersionWorkContext,
    VersionWorkKind,
    build_funds_port,
)
from course_supporter.homework.path_config import PathKey, SubmissionState
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.models.source import AssignmentType
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingMovement,
    BillingReservation,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

E = Decimal("0.05")
P = 102
TWO_E = 170
ACTOR = "test-admin"


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    course_id: uuid.UUID
    task_id: uuid.UUID
    student_id: uuid.UUID
    submission_id: uuid.UUID
    homework_job_id: uuid.UUID
    processing_job_id: uuid.UUID
    student: uuid.UUID
    author: uuid.UUID


_WORLDS: list[uuid.UUID] = []


@pytest.fixture(autouse=True)
async def _drop_billing(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    """Each test's billing rows go (the migration tests walk the schema down)."""
    yield
    while _WORLDS:
        await drop_billing_of_tenant(session_factory, _WORLDS.pop())


@pytest.fixture(autouse=True)
def _charging_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Charging on, and the path's test mode off (tests of test mode undo it)."""
    monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: True)
    monkeypatch.setattr(funds, "in_test_mode", lambda *a, **k: False)


async def make_world(
    session_factory: async_sessionmaker[AsyncSession], *, task_type: str = "task"
) -> World:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-e-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        course = make_root_course_node(tenant_id=tenant.id, title="Курс", order=0)
        session.add(course)
        await session.flush()
        task = AuthoredDocument(
            course_node_id=course.id,
            course_root_id=course.id,
            source_type="text",
            source_url="https://example.com/task",
            filename="task.txt",
            task_type=task_type,
        )
        student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:8]}")
        session.add_all([task, student])
        await session.flush()
        submission = HomeworkSubmission(
            tenant_id=tenant.id,
            student_id=student.id,
            course_node_id=course.id,
            node_id=course.id,
            authored_document_id=task.id,
            file_url="s3://bucket/work.py",
            file_type="text/plain",
            original_filename="work.py",
            status="received",
        )
        session.add(submission)
        await session.flush()
        homework_job = _job(
            tenant.id, course.id, JobType.HOMEWORK_PROCESSING, submission.id
        )
        processing_job = _job(
            tenant.id, course.id, JobType.DOCUMENT_PROCESSING, task.id
        )
        session.add_all([homework_job, processing_job])
        student_account = await get_or_create_student_account(
            session, tenant_id=tenant.id, student_id=student.id
        )
        author_account = await get_or_create_author_account(
            session, tenant_id=tenant.id
        )
        await session.commit()
        _WORLDS.append(tenant.id)
        return World(
            tenant.id,
            course.id,
            task.id,
            student.id,
            submission.id,
            homework_job.id,
            processing_job.id,
            student_account.id,
            author_account.id,
        )


def _job(
    tenant_id: uuid.UUID, node_id: uuid.UUID, job_type: JobType, subject_id: uuid.UUID
) -> Job:
    return Job(
        tenant_id=tenant_id,
        course_node_id=node_id,
        job_type=job_type.value,
        subject_type=JOB_SUBJECT_TYPE[job_type],
        subject_id=subject_id,
        status="active",
    )


async def call(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    usd: str,
    *,
    action: str = "criteria_evaluation",
) -> None:
    """One priced call of the job in the register."""
    async with session_factory() as session:
        session.add(
            ExternalServiceCall(
                job_id=job_id,
                action=action,
                provider="gemini",
                model_id="gemini-2.5-flash",
                success=True,
                unit_type="tokens",
                unit_in=10,
                unit_out=10,
                cost_usd=Decimal(usd),
                cost_kind=CostKind.ESTIMATED.value,
                call_started_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def buy(
    session_factory: async_sessionmaker[AsyncSession], account: uuid.UUID, units: int
) -> None:
    async with session_factory() as session:
        await ledger.credit_purchase(
            session,
            account_id=account,
            units=units,
            amount_paid_minor=500,
            currency="EUR",
            provider="fake",
        )
        await session.commit()


async def balance(
    session_factory: async_sessionmaker[AsyncSession], account: uuid.UUID
) -> ledger.Balance:
    async with session_factory() as session:
        return await ledger.balance(session, account)


async def sums(
    session_factory: async_sessionmaker[AsyncSession], kind: ActionKind, key: str
) -> dict[str, int]:
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingMovement.kind, func.sum(BillingMovement.units))
            .where(
                BillingMovement.action_kind == kind.value,
                BillingMovement.action_key == key,
            )
            .group_by(BillingMovement.kind)
        )
        return {k: int(u) for k, u in rows.tuples()}


async def reservations(
    session_factory: async_sessionmaker[AsyncSession], kind: ActionKind, key: str
) -> list[BillingReservation]:
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingReservation).where(
                BillingReservation.action_kind == kind.value,
                BillingReservation.action_key == key,
            )
        )
        return list(rows.scalars())


async def movements_of_tenant(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count(BillingMovement.id)).where(
                    BillingMovement.account_id.in_([world.student, world.author])
                )
            )
            or 0
        )


async def set_credit(
    session_factory: async_sessionmaker[AsyncSession], world: World, **changes: Any
) -> None:
    async with session_factory() as session:
        await admin.update_author_settings(
            session, world.tenant_id, actor=ACTOR, **changes
        )
        await session.commit()


async def course_rule(
    session_factory: async_sessionmaker[AsyncSession],
    world: World,
    kind: ActionKind = ActionKind.REVIEW,
) -> None:
    async with session_factory() as session:
        await admin.create_payment_rule(
            session,
            actor=ACTOR,
            tenant_id=world.tenant_id,
            scope=RuleScope.COURSE,
            action_kind=kind,
            course_node_id=world.course_id,
        )
        await session.commit()


def review(
    world: World, task_type: AssignmentType = AssignmentType.TASK
) -> SubmissionContext:
    return SubmissionContext(
        tenant_id=world.tenant_id,
        student_id=world.student_id,
        submission_id=world.submission_id,
        path_key=PathKey(task_type, SubmissionState.FIRST),
        expected_usd=E,
    )


def key_of(world: World) -> str:
    return str(world.submission_id)


# ── The student's own review ──────────────────────────────────────────


class TestStudentReview:
    async def test_reserve_at_start_fact_at_end_rest_released(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.student, 500)
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w), 1.0)

        assert answer.decision is FundsDecision.ALLOWED
        (held,) = await reservations(session_factory, ActionKind.REVIEW, key_of(w))
        assert held.units == TWO_E, "min(2E, ceiling, available)"
        assert (await balance(session_factory, w.student)).available == 500 - TWO_E

        await call(session_factory, w.homework_job_id, "0.04")
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert moved[MovementKind.DEBIT.value] == 68
        assert moved[MovementKind.RESERVE_RELEASE.value] == TWO_E - 68
        after = await balance(session_factory, w.student)
        assert (after.balance, after.available, after.difference) == (432, 432, 0)

    async def test_the_ceiling_caps_the_reserve(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.student, 500)
        port = funds.LedgerFundsPort(session_factory)

        await port.check_and_reserve(review(w), 0.07)  # 0.07 x 1700 = 119 < 170

        (held,) = await reservations(session_factory, ActionKind.REVIEW, key_of(w))
        assert held.units == 119
        await port.release_remainder(review(w), SubmissionOutcome.FAILED)

    async def test_a_failure_releases_everything(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.student, 500)
        port = funds.LedgerFundsPort(session_factory)
        await port.check_and_reserve(review(w), 1.0)
        await call(session_factory, w.homework_job_id, "0.04")

        await port.release_remainder(review(w), SubmissionOutcome.FAILED)

        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert MovementKind.DEBIT.value not in moved, "a failure is charged nothing"
        assert moved[MovementKind.RESERVE_RELEASE.value] == TWO_E
        assert (await balance(session_factory, w.student)).available == 500

    async def test_a_retry_reserves_and_charges_once(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.student, 500)
        port = funds.LedgerFundsPort(session_factory)

        await port.check_and_reserve(review(w), 1.0)
        await port.check_and_reserve(review(w), 1.0)  # ARQ re-ran the job
        await call(session_factory, w.homework_job_id, "0.04")
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        assert (
            len(await reservations(session_factory, ActionKind.REVIEW, key_of(w))) == 1
        )
        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert moved[MovementKind.RESERVE.value] == TWO_E
        assert moved[MovementKind.DEBIT.value] == 68
        assert (await balance(session_factory, w.student)).available == 432

    async def test_the_fact_leaves_out_the_task_preparation(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.student, 500)
        port = funds.LedgerFundsPort(session_factory)
        await port.check_and_reserve(review(w), 1.0)
        await call(session_factory, w.homework_job_id, "0.04")
        await call(
            session_factory, w.homework_job_id, "0.50", action="criteria_decomposition"
        )

        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert moved[MovementKind.DEBIT.value] == 68, "the author's call is not F"

    async def test_short_of_the_price_is_refused_and_nothing_held(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.student, P - 1)
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w), 1.0)

        assert answer.decision is FundsDecision.REFUSED
        assert await reservations(session_factory, ActionKind.REVIEW, key_of(w)) == []

    async def test_cost_over_reserve_is_the_platforms_and_the_student_is_told(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.student, 120)  # enough for P, less than 2E
        port = funds.LedgerFundsPort(session_factory)
        await port.check_and_reserve(review(w), 1.0)
        await call(session_factory, w.homework_job_id, "0.10")  # F = 170

        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert moved[MovementKind.DEBIT.value] == 120, "min(F, R)"
        assert moved[MovementKind.PLATFORM_COVERED.value] == 50, "F - R"
        assert MovementKind.DIFFERENCE_ARISEN.value not in moved
        after = await balance(session_factory, w.student)
        assert (after.balance, after.difference) == (0, 0)
        async with session_factory() as session:
            note = await funds.billing_note(session, w.submission_id)
            platform = (
                await session.execute(
                    select(BillingMovement.payer_source).where(
                        BillingMovement.action_key == key_of(w),
                        BillingMovement.kind == MovementKind.PLATFORM_COVERED.value,
                    )
                )
            ).scalar_one()
        assert note == {
            "kind": "cost_over_reserve",
            "cost_units": 170,
            "charged_units": 120,
        }
        assert platform == PayerSource.PLATFORM.value

    async def test_no_note_within_the_reserve(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.student, 500)
        port = funds.LedgerFundsPort(session_factory)
        await port.check_and_reserve(review(w), 1.0)
        await call(session_factory, w.homework_job_id, "0.04")
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        async with session_factory() as session:
            assert await funds.billing_note(session, w.submission_id) is None

    async def test_a_reserve_released_while_held_charges_nothing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """S3: a hold outlived the stale-reserve threshold; the continuation
        is charged nothing (WARNING ``billing_reserve_released_before_end``)."""
        w = await make_world(session_factory)
        await buy(session_factory, w.student, 500)
        port = funds.LedgerFundsPort(session_factory)
        await port.check_and_reserve(review(w), 1.0)
        async with session_factory() as session:
            released = await release_stale_reservations(
                session,
                datetime.now(UTC) + timedelta(days=2),
                older_than=timedelta(hours=24),
            )
            await session.commit()
        assert any(r.action_key == key_of(w) for r in released)
        await call(session_factory, w.homework_job_id, "0.04")

        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert MovementKind.DEBIT.value not in moved
        assert (await balance(session_factory, w.student)).available == 500


class TestTests:
    """A test (kind ``test``): free while the payer's balance is above 0."""

    async def test_without_balance_a_test_cannot_be_sent(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, task_type="test")
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w, AssignmentType.TEST), 0.0)

        assert answer.decision is FundsDecision.REFUSED

    async def test_with_balance_it_is_free(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, task_type="test")
        await buy(session_factory, w.student, 1)
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w, AssignmentType.TEST), 0.0)
        await port.release_remainder(
            review(w, AssignmentType.TEST), SubmissionOutcome.COMPLETED
        )

        assert answer.decision is FundsDecision.ALLOWED
        assert await reservations(session_factory, ActionKind.REVIEW, key_of(w)) == []
        assert (await balance(session_factory, w.student)).available == 1


class TestPathTestMode:
    async def test_nothing_is_reserved_or_charged(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(funds, "in_test_mode", lambda *a, **k: True)
        w = await make_world(session_factory)
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w), 1.0)  # no units at all
        await call(session_factory, w.homework_job_id, "0.04")
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        assert answer.decision is FundsDecision.ALLOWED
        assert await movements_of_tenant(session_factory, w) == 0


# ── The author pays under a payment rule ─────────────────────────────


class TestAuthorPaysForTheStudent:
    async def test_no_units_never_stop_the_review_and_the_platform_covers(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await course_rule(session_factory, w)
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w), 1.0)

        assert answer.decision is FundsDecision.ALLOWED
        (held,) = await reservations(session_factory, ActionKind.REVIEW, key_of(w))
        assert (held.account_id, held.units) == (w.author, 0)

        await call(session_factory, w.homework_job_id, "0.04")
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert moved == {MovementKind.PLATFORM_COVERED.value: 68}
        assert (await balance(session_factory, w.author)).difference == 0
        async with session_factory() as session:
            assert await funds.billing_note(session, w.submission_id) is None, (
                "what the author pays is not the student's to read"
            )

    async def test_an_author_with_credit_runs_into_a_difference(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await course_rule(session_factory, w)
        await set_credit(session_factory, w, credit_allowed=True)
        port = funds.LedgerFundsPort(session_factory)

        await port.check_and_reserve(review(w), 1.0)
        await call(session_factory, w.homework_job_id, "0.04")
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert moved == {MovementKind.DIFFERENCE_ARISEN.value: 68}
        assert (await balance(session_factory, w.author)).difference == 68

    async def test_without_a_rule_the_student_pays(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.author, 1000)  # the author's units are not used
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w), 1.0)

        assert answer.decision is FundsDecision.REFUSED
        assert (await balance(session_factory, w.author)).available == 1000


# ── Task preparation: key explanations, criteria decomposition ───────


def version_work(world: World, *, by_submission: bool) -> VersionWorkContext:
    return VersionWorkContext(
        tenant_id=world.tenant_id,
        authored_document_id=world.task_id,
        source_content_hash="hash",
        work_kind=VersionWorkKind.KEY_EXPLANATION,
        reference_id=uuid.uuid4(),
        requested_by_submission=by_submission,
        expected_usd=E,
    )


class TestKeyExplanations:
    async def test_the_authors_own_request_is_refused_without_units(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, task_type="test")
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve_for_version(
            version_work(w, by_submission=False), 0.08
        )

        assert answer.decision is FundsDecision.REFUSED

    async def test_a_submissions_request_goes_on_at_zero(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, task_type="test")
        port = funds.LedgerFundsPort(session_factory)
        work = version_work(w, by_submission=True)

        answer = await port.check_and_reserve_for_version(work, 0.08)
        await port.account_version_work_cost(work, 0.02)

        assert answer.decision is FundsDecision.ALLOWED
        key = f"{w.task_id}:{work.reference_id}:key_explanation"
        assert await sums(session_factory, ActionKind.TASK_PREPARATION, key) == {
            MovementKind.PLATFORM_COVERED.value: 34
        }

    async def test_an_author_with_credit_goes_on_and_owes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, task_type="test")
        await set_credit(session_factory, w, credit_allowed=True)
        port = funds.LedgerFundsPort(session_factory)
        work = version_work(w, by_submission=False)

        answer = await port.check_and_reserve_for_version(work, 0.08)
        await port.account_version_work_cost(work, 0.02)

        assert answer.decision is FundsDecision.ALLOWED
        assert (await balance(session_factory, w.author)).difference == 34

    async def test_a_failed_generation_is_released_and_charged_nothing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, task_type="test")
        await buy(session_factory, w.author, 500)
        port = funds.LedgerFundsPort(session_factory)
        work = version_work(w, by_submission=False)

        await port.check_and_reserve_for_version(work, 0.08)
        await port.account_version_work_cost(work, 0.02, SubmissionOutcome.FAILED)

        key = f"{w.task_id}:{work.reference_id}:key_explanation"
        moved = await sums(session_factory, ActionKind.TASK_PREPARATION, key)
        assert MovementKind.DEBIT.value not in moved
        assert (await balance(session_factory, w.author)).available == 500


class TestCriteriaDecomposition:
    async def test_a_submissions_decomposition_goes_on_at_zero(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(funds, "preparation_e_usd", lambda **_: E)
        w = await make_world(session_factory)
        async with session_factory() as session:
            task = await session.get(AuthoredDocument, w.task_id)
        assert task is not None

        charge = await funds.preparation_started(
            session_factory,
            document=task,
            kind="criteria_decomposition",
            version="content-hash",
            task_chars=1000,
        )
        assert charge is not None
        (held,) = await reservations(
            session_factory, ActionKind.TASK_PREPARATION, charge.action_key
        )
        assert held.units == 0

        await funds.preparation_ended(session_factory, charge, fact_usd=Decimal("0.04"))

        moved = await sums(
            session_factory, ActionKind.TASK_PREPARATION, charge.action_key
        )
        assert moved == {MovementKind.PLATFORM_COVERED.value: 68}


# ── Processing of a material ──────────────────────────────────────────


async def start_processing(
    session_factory: async_sessionmaker[AsyncSession], w: World
) -> None:
    await funds.processing_started(
        kind="text",
        factors=MaterialFactors(text_chars=1000, safety_chars=1000),
        job_id=w.processing_job_id,
        tenant_id=w.tenant_id,
        authored_document_id=w.task_id,
        session_factory=session_factory,
    )


class TestProcessing:
    @pytest.fixture(autouse=True)
    def _priced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(funds, "processing_e_usd", lambda **_: E)

    async def test_an_author_without_credit_short_of_the_price_fails_the_job(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.author, P - 1)

        with pytest.raises(funds.InsufficientUnitsError) as raised:
            await start_processing(session_factory, w)

        assert raised.value.category is ErrorCategory.INSUFFICIENT_UNITS
        key = str(w.processing_job_id)
        assert await reservations(session_factory, ActionKind.PROCESSING, key) == []

    async def test_an_author_with_credit_goes_on_without_units(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await set_credit(session_factory, w, credit_allowed=True)

        await start_processing(session_factory, w)
        await call(session_factory, w.processing_job_id, "0.04")
        await funds.job_ended(
            session_factory,
            job_id=w.processing_job_id,
            authored_document_id=w.task_id,
            completed=True,
        )

        moved = await sums(
            session_factory, ActionKind.PROCESSING, str(w.processing_job_id)
        )
        assert moved == {MovementKind.DIFFERENCE_ARISEN.value: 68}

    async def test_reserve_then_fact_then_failure_releases(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.author, 500)

        await start_processing(session_factory, w)
        await start_processing(session_factory, w)  # a retry: one reserve
        key = str(w.processing_job_id)
        (held,) = await reservations(session_factory, ActionKind.PROCESSING, key)
        assert held.units == TWO_E

        await funds.job_ended(
            session_factory,
            job_id=w.processing_job_id,
            authored_document_id=w.task_id,
            completed=False,
        )

        moved = await sums(session_factory, ActionKind.PROCESSING, key)
        assert moved == {
            MovementKind.RESERVE.value: TWO_E,
            MovementKind.RESERVE_RELEASE.value: TWO_E,
        }
        assert (await balance(session_factory, w.author)).available == 500

    async def test_the_benefit_splits_at_its_limit(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.author, 500)
        await set_credit(
            session_factory, w, benefit=Benefit.MONTHLY_LIMIT, monthly_limit_units=300
        )
        async with session_factory() as session:
            left = (
                await benefit.benefit_allowance(
                    session, w.author, datetime.now(UTC).date()
                )
            ).remaining_units
        assert left > 0
        await start_processing(session_factory, w)
        usd = Decimal(left + 10) / Decimal(1700)  # F = what the limit has left + 10
        await call(
            session_factory, w.processing_job_id, str(usd.quantize(Decimal("1e-9")))
        )

        await funds.job_ended(
            session_factory,
            job_id=w.processing_job_id,
            authored_document_id=w.task_id,
            completed=True,
        )

        moved = await sums(
            session_factory, ActionKind.PROCESSING, str(w.processing_job_id)
        )
        assert moved[MovementKind.BENEFIT_COVERED.value] == left
        assert moved[MovementKind.DEBIT.value] == 10


# ── The switch ────────────────────────────────────────────────────────


class TestTheSwitch:
    async def test_off_the_journal_stays_empty(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: False)
        monkeypatch.setattr(funds, "processing_e_usd", lambda **_: E)
        monkeypatch.setattr(funds, "preparation_e_usd", lambda **_: E)
        w = await make_world(session_factory)
        port = build_funds_port(session_factory)
        async with session_factory() as session:
            task = await session.get(AuthoredDocument, w.task_id)
        assert task is not None

        assert isinstance(port, AlwaysEnoughFundsPort)
        assert (await port.check_and_reserve(review(w), 1.0)).decision is (
            FundsDecision.ALLOWED
        ), "no refusal for want of units"
        await start_processing(session_factory, w)  # an author with nothing
        charge = await funds.preparation_started(
            session_factory,
            document=task,
            kind="criteria_decomposition",
            version="v",
            task_chars=10,
        )
        await funds.job_ended(
            session_factory,
            job_id=w.processing_job_id,
            authored_document_id=w.task_id,
            completed=True,
        )

        assert charge is None
        assert await movements_of_tenant(session_factory, w) == 0

    async def test_on_the_port_is_the_journals(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        assert isinstance(build_funds_port(session_factory), funds.LedgerFundsPort)
