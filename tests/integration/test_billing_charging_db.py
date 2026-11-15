"""BIL-E on a live database: every paid action reserves, is charged its fact, releases.

The rules are in ``billing/charging.py``; the wiring in ``billing/funds.py``.
Numbers: K = 900 units a dollar and a margin of 0.20
(``config/cost_estimate.yaml``), E = $0.05 → P = ceil(0.05 x 1.2 x 900) = 54,
2E = 90 units; a call of $0.04 is F = 36 units.

The port and the gates open sessions of their own and commit, so each test
builds a world of its own (a fresh tenant) and ends every reserve it makes.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.billing import (
    admin,
    benefit,
    charging,
    estimates,
    funds,
    ledger,
    quotes,
)
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
    FundsRefusalReason,
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
    ActionCostEstimate,
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
P = 54
TWO_E = 90
ACTOR = "test-admin"
ESTIMATE_ROW: dict[str, Any] = {
    "e_usd": E,
    "reserve_units": TWO_E,
    "margin": Decimal("0.2"),
    "units_per_usd": Decimal(900),
    "breakdown": [],
    "inputs": {},
    "uncalibrated": True,
}
"""The rest of an estimate row whose P a test sets."""


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
        assert moved[MovementKind.DEBIT.value] == 36
        assert moved[MovementKind.RESERVE_RELEASE.value] == TWO_E - 36
        after = await balance(session_factory, w.student)
        assert (after.balance, after.available, after.difference) == (464, 464, 0)

    async def test_the_ceiling_caps_the_reserve(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.student, 500)
        port = funds.LedgerFundsPort(session_factory)

        await port.check_and_reserve(review(w), 0.07)  # 0.07 x 900 = 63 < 90

        (held,) = await reservations(session_factory, ActionKind.REVIEW, key_of(w))
        assert held.units == 63
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
        assert moved[MovementKind.DEBIT.value] == 36
        assert (await balance(session_factory, w.student)).available == 464

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
        assert moved[MovementKind.DEBIT.value] == 36, "the author's call is not F"

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
        await buy(session_factory, w.student, 70)  # enough for P, less than 2E
        port = funds.LedgerFundsPort(session_factory)
        await port.check_and_reserve(review(w), 1.0)
        await call(session_factory, w.homework_job_id, "0.10")  # F = 90

        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert moved[MovementKind.DEBIT.value] == 70, "min(F, R)"
        assert moved[MovementKind.PLATFORM_COVERED.value] == 20, "F - R"
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
            "cost_units": 90,
            "charged_units": 70,
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


class TestTestsUnderTheAuthorsRule:
    async def test_with_a_balance_of_zero_it_waits_with_credit_it_goes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """As for a student: free while the author's balance is above 0."""
        w = await make_world(session_factory, task_type="test")
        await course_rule(session_factory, w)
        port = funds.LedgerFundsPort(session_factory)
        context = review(w, AssignmentType.TEST)

        empty = await port.check_and_reserve(context, 1.0)
        await set_credit(session_factory, w, credit_allowed=True)
        credit = await port.check_and_reserve(context, 1.0)

        assert empty.decision is FundsDecision.REFUSED
        assert credit.decision is FundsDecision.ALLOWED
        assert await movements_of_tenant(session_factory, w) == 0

    async def test_with_a_balance_it_is_free(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, task_type="test")
        await course_rule(session_factory, w)
        await buy(session_factory, w.author, 1)

        answer = await funds.LedgerFundsPort(session_factory).check_and_reserve(
            review(w, AssignmentType.TEST), 1.0
        )

        assert answer.decision is FundsDecision.ALLOWED
        assert (await balance(session_factory, w.author)).available == 1


CEILING_USD = 0.89
"""The money ceiling of the ``task`` path a first submission takes
(config/submission_paths.yaml): N = ceil(0.89 x 900) = 801."""
N = 801


async def typical_price(
    session_factory: async_sessionmaker[AsyncSession], w: World
) -> int:
    """The task's typical price (basis ``typical``), as a screen shows it."""
    async with session_factory() as session:
        task = await session.get(AuthoredDocument, w.task_id)
        assert task is not None
        found = await quotes.review_price(
            session, task=task, tenant_id=w.tenant_id, student_id=w.student_id
        )
        await session.commit()
    assert found.basis == "typical"
    assert 0 < found.units < N, "the test needs room between the two"
    return found.units


def _no_estimate(*_: Any, **__: Any) -> Any:
    raise RuntimeError("estimate broke")


class TestTheCeiling:
    """An estimate fallen to the ceiling (``expected_usd`` None; operator
    2026-11-15): the start needs the task's typical price, else a balance
    above 0; the reserve is min(N, available); the excess is the platform's."""

    async def test_between_the_typical_price_and_n_it_starts_with_what_is_there(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        p_typ = await typical_price(session_factory, w)
        context = replace(review(w), expected_usd=None)
        port = funds.LedgerFundsPort(session_factory)
        await buy(session_factory, w.student, p_typ + 10)

        answer = await port.check_and_reserve(context, CEILING_USD)

        assert answer.decision is FundsDecision.ALLOWED, "N is not needed"
        (held,) = await reservations(session_factory, ActionKind.REVIEW, key_of(w))
        assert held.units == p_typ + 10, "min(N, available)"

        await call(session_factory, w.homework_job_id, "0.50")  # F = 450
        await port.release_remainder(context, SubmissionOutcome.COMPLETED)

        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert moved[MovementKind.DEBIT.value] == p_typ + 10
        assert moved[MovementKind.PLATFORM_COVERED.value] == 450 - (p_typ + 10)

    async def test_below_the_typical_price_it_is_held(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        p_typ = await typical_price(session_factory, w)
        await buy(session_factory, w.student, p_typ - 1)

        answer = await funds.LedgerFundsPort(session_factory).check_and_reserve(
            replace(review(w), expected_usd=None), CEILING_USD
        )

        assert answer.decision is FundsDecision.REFUSED
        assert await reservations(session_factory, ActionKind.REVIEW, key_of(w)) == []

    async def test_neither_priced_a_balance_above_zero_is_enough(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(estimates, "estimate_review", _no_estimate)
        w = await make_world(session_factory)
        context = replace(review(w), expected_usd=None)
        port = funds.LedgerFundsPort(session_factory)

        empty = await port.check_and_reserve(context, CEILING_USD)
        await buy(session_factory, w.student, 1)
        one = await port.check_and_reserve(context, CEILING_USD)

        assert empty.decision is FundsDecision.REFUSED
        assert one.decision is FundsDecision.ALLOWED
        (held,) = await reservations(session_factory, ActionKind.REVIEW, key_of(w))
        assert held.units == 1
        await port.release_remainder(context, SubmissionOutcome.FAILED)

    async def test_the_author_under_a_rule_by_the_same_rule(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await course_rule(session_factory, w)
        p_typ = await typical_price(session_factory, w)
        context = replace(review(w), expected_usd=None)
        port = funds.LedgerFundsPort(session_factory)
        await buy(session_factory, w.author, p_typ - 1)

        short = await port.check_and_reserve(context, CEILING_USD)
        await buy(session_factory, w.author, 11)
        enough = await port.check_and_reserve(context, CEILING_USD)

        assert short.decision is FundsDecision.REFUSED
        assert enough.decision is FundsDecision.ALLOWED
        (held,) = await reservations(session_factory, ActionKind.REVIEW, key_of(w))
        assert (held.account_id, held.units) == (w.author, p_typ + 10)
        await port.release_remainder(context, SubmissionOutcome.FAILED)

    async def test_a_held_review_without_an_estimate_continues_by_the_same_rule(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No estimate row: the typical price; neither priced: a balance > 0."""
        from course_supporter.billing.resume_funded import can_pay

        w = await make_world(session_factory)
        p_typ = await typical_price(session_factory, w)
        await buy(session_factory, w.student, p_typ - 1)

        async def payable() -> bool:
            async with session_factory() as session:
                submission = await session.get(HomeworkSubmission, w.submission_id)
                assert submission is not None
                return await can_pay(session, submission)

        short = await payable()
        await buy(session_factory, w.student, 1)
        typical = await payable()
        monkeypatch.setattr(estimates, "estimate_review", _no_estimate)
        neither = await payable()

        assert (short, typical, neither) == (False, True, True)

    async def test_neither_priced_a_held_review_waits_at_zero(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from course_supporter.billing.resume_funded import can_pay

        monkeypatch.setattr(estimates, "estimate_review", _no_estimate)
        w = await make_world(session_factory)
        async with session_factory() as session:
            submission = await session.get(HomeworkSubmission, w.submission_id)
            assert submission is not None
            assert await can_pay(session, submission) is False

    def test_n_is_the_cap_of_every_reserve(self) -> None:
        assert charging.ceiling_units(Decimal("0.10")) == 90
        assert charging.reserve_request(Decimal(1), Decimal("0.10")) == 90
        assert charging.ceiling_units(Decimal(str(CEILING_USD))) == N


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
    """BIL-E2, 5a item 2: the platform does not work on credit — a review the
    author pays for waits for the author's units, as a student's for theirs."""

    async def test_an_author_without_units_holds_the_review_and_holds_nothing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await course_rule(session_factory, w)
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w), 1.0)

        assert answer.decision is FundsDecision.REFUSED
        assert answer.refusal_reason is FundsRefusalReason.INSUFFICIENT_FUNDS
        assert await reservations(session_factory, ActionKind.REVIEW, key_of(w)) == []
        assert await movements_of_tenant(session_factory, w) == 0

    async def test_short_of_the_price_holds_it_too(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory)
        await course_rule(session_factory, w)
        await buy(session_factory, w.author, P - 1)

        answer = await funds.LedgerFundsPort(session_factory).check_and_reserve(
            review(w), 1.0
        )

        assert answer.decision is FundsDecision.REFUSED

    async def test_with_units_the_author_pays_and_the_platform_covers_the_excess(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Started, a review goes to its end: what it costs over the author's
        reserve is the platform's, never a debt (excess_to_platform)."""
        w = await make_world(session_factory)
        await course_rule(session_factory, w)
        await buy(session_factory, w.author, P)
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w), 1.0)

        assert answer.decision is FundsDecision.ALLOWED
        (held,) = await reservations(session_factory, ActionKind.REVIEW, key_of(w))
        assert (held.account_id, held.units) == (w.author, P)

        await call(session_factory, w.homework_job_id, "0.10")  # F = 90
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        moved = await sums(session_factory, ActionKind.REVIEW, key_of(w))
        assert moved == {
            MovementKind.RESERVE.value: P,
            MovementKind.DEBIT.value: P,
            MovementKind.PLATFORM_COVERED.value: 90 - P,
        }
        assert (await balance(session_factory, w.author)).difference == 0
        assert (await balance(session_factory, w.student)).available == 0
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
        assert moved == {MovementKind.DIFFERENCE_ARISEN.value: 36}
        assert (await balance(session_factory, w.author)).difference == 36

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
            MovementKind.PLATFORM_COVERED.value: 18
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
        assert (await balance(session_factory, w.author)).difference == 18

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
        assert moved == {MovementKind.PLATFORM_COVERED.value: 36}


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
        assert moved == {MovementKind.DIFFERENCE_ARISEN.value: 36}

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
        usd = Decimal(left + 10) / Decimal(900)  # F = what the limit has left + 10
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


# ── A5: units spent between the submission and its start ──────────────


class TestHeldForFunds:
    async def test_held_then_continued_once_the_student_can_pay(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The run was refused (``awaiting_funds``, nothing lost); a purchase
        that is not enough continues nothing; one that is — a new job."""
        from unittest.mock import AsyncMock, MagicMock

        from course_supporter.billing.resume_funded import resume_funded

        w = await make_world(session_factory)
        await buy(session_factory, w.student, 1)
        async with session_factory() as session:
            await session.execute(
                update(HomeworkSubmission)
                .where(HomeworkSubmission.id == w.submission_id)
                .values(status="awaiting_funds")
            )
            await session.execute(
                update(Job).where(Job.id == w.homework_job_id).values(status="complete")
            )
            await session.commit()
        arq = AsyncMock()
        arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq:hw:resume"))

        async with session_factory() as session:
            short = await resume_funded(session, arq, dry_run=False)
        await buy(session_factory, w.student, 10_000)
        async with session_factory() as session:
            funded = await resume_funded(session, arq, dry_run=False)
        async with session_factory() as session:
            again = await resume_funded(session, arq, dry_run=False)
            jobs = await session.scalar(
                select(func.count(Job.id)).where(
                    Job.subject_id == w.submission_id, Job.status == "queued"
                )
            )
            status = await session.scalar(
                select(HomeworkSubmission.status).where(
                    HomeworkSubmission.id == w.submission_id
                )
            )

        assert w.submission_id not in short.resumed
        assert w.submission_id in funded.resumed
        assert w.submission_id not in again.resumed, "a job is in flight already"
        assert jobs == 1
        assert status == "awaiting_funds", "the run of the new job lifts the hold"


class TestHeldForTheAuthor:
    async def _hold(
        self, session_factory: async_sessionmaker[AsyncSession], w: World
    ) -> None:
        async with session_factory() as session:
            await session.execute(
                update(HomeworkSubmission)
                .where(HomeworkSubmission.id == w.submission_id)
                .values(status="awaiting_funds")
            )
            await session.execute(
                update(Job).where(Job.id == w.homework_job_id).values(status="complete")
            )
            await session.commit()

    async def test_held_then_continued_once_the_author_tops_up(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from course_supporter.billing.held import waiting_for_author
        from course_supporter.billing.resume_funded import can_pay, resume_funded

        w = await make_world(session_factory)
        await course_rule(session_factory, w)
        await buy(session_factory, w.student, 10_000)  # the student's are not used
        await self._hold(session_factory, w)
        arq = AsyncMock()
        arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq:hw:resume"))

        async with session_factory() as session:
            submission = await session.get(HomeworkSubmission, w.submission_id)
            assert submission is not None
            waiting = await waiting_for_author(session, [submission])
            short = await resume_funded(session, arq, dry_run=False)
        await buy(session_factory, w.author, 10_000)
        async with session_factory() as session:
            submission = await session.get(HomeworkSubmission, w.submission_id)
            assert submission is not None
            payable = await can_pay(session, submission)
            funded = await resume_funded(session, arq, dry_run=False)

        assert waiting == {w.submission_id}
        assert w.submission_id not in short.resumed
        assert payable is True
        assert w.submission_id in funded.resumed

    async def test_an_author_with_credit_is_continued_at_once(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from course_supporter.billing.resume_funded import can_pay

        w = await make_world(session_factory)
        await course_rule(session_factory, w)
        await set_credit(session_factory, w, credit_allowed=True)
        await self._hold(session_factory, w)
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w), 1.0)
        async with session_factory() as session:
            submission = await session.get(HomeworkSubmission, w.submission_id)
            assert submission is not None
            payable = await can_pay(session, submission)

        assert answer.decision is FundsDecision.ALLOWED, "credit: the review goes"
        assert payable is True
        await port.release_remainder(review(w), SubmissionOutcome.FAILED)

    async def test_a_student_held_for_their_own_units_is_not_the_authors(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from course_supporter.billing.held import held_for_author, waiting_for_author

        w = await make_world(session_factory)
        await self._hold(session_factory, w)
        async with session_factory() as session:
            submission = await session.get(HomeworkSubmission, w.submission_id)
            assert submission is not None
            waiting = await waiting_for_author(session, [submission])
            held = await held_for_author(
                session, tenant_id=w.tenant_id, account_id=w.author
            )

        assert waiting == frozenset()
        assert held.count == 0

    async def test_the_authors_count_their_missing_units_and_no_one_elses(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from course_supporter.billing.held import held_for_author

        w = await make_world(session_factory)
        other = await make_world(session_factory)
        for world in (w, other):
            await course_rule(session_factory, world)
            await self._hold(session_factory, world)
        await buy(session_factory, w.author, 10)
        async with session_factory() as session:
            for world, p_units in ((w, 150), (other, 999)):
                session.add(
                    ActionCostEstimate(
                        action_kind=ActionKind.REVIEW.value,
                        action_key=str(world.submission_id),
                        tenant_id=world.tenant_id,
                        submission_id=world.submission_id,
                        p_units=p_units,
                        **ESTIMATE_ROW,
                    )
                )
            await session.commit()
        async with session_factory() as session:
            held = await held_for_author(
                session, tenant_id=w.tenant_id, account_id=w.author
            )
            created = await session.scalar(
                select(HomeworkSubmission.created_at).where(
                    HomeworkSubmission.id == w.submission_id
                )
            )
            await session.execute(
                delete(ActionCostEstimate).where(
                    ActionCostEstimate.action_key.in_(
                        [str(w.submission_id), str(other.submission_id)]
                    )
                )
            )
            await session.commit()

        assert (held.count, held.missing_units) == (1, 150 - 10)
        assert held.oldest_at == created


async def test_off_nothing_is_held_for_the_author(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Charging off (production today): the author's empty account holds nothing."""
    monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: False)
    w = await make_world(session_factory)
    await course_rule(session_factory, w)

    answer = await build_funds_port(session_factory).check_and_reserve(review(w), 1.0)

    assert answer.decision is FundsDecision.ALLOWED
    assert await movements_of_tenant(session_factory, w) == 0


async def test_the_portal_says_whose_units_a_held_review_waits_for(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """BIL-E2, 5a item 2: ``awaiting_author_funds`` on the detail and the list;
    ``awaiting_funds`` when the student pays."""
    from httpx import ASGITransport, AsyncClient

    from course_supporter.api.app import app
    from course_supporter.api.deps import get_current_student
    from course_supporter.auth.context import StudentContext
    from course_supporter.storage.database import get_session

    w = await make_world(session_factory)
    async with session_factory() as session:
        await session.execute(
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == w.submission_id)
            .values(status="awaiting_funds")
        )
        await session.commit()

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_student] = lambda: StudentContext(
        student_id=w.student_id, tenant_id=w.tenant_id, login="s", display_name=None
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            own = await client.get(f"/api/v1/portal/submissions/{w.submission_id}")
            await course_rule(session_factory, w)
            detail = await client.get(f"/api/v1/portal/submissions/{w.submission_id}")
            listed = await client.get(f"/api/v1/portal/tasks/{w.task_id}/submissions")
    finally:
        app.dependency_overrides.clear()

    assert own.status_code == 200, own.text
    assert own.json()["presentation"] == {
        "state": "awaiting_funds",
        "reason_code": "awaiting_funds",
    }
    assert detail.json()["presentation"] == {
        "state": "awaiting_funds",
        "reason_code": "awaiting_author_funds",
    }
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["presentation"]["reason_code"] == "awaiting_author_funds"


async def test_the_portal_detail_carries_the_billing_note(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A3: ``GET /portal/submissions/{id}`` — ``billing_note`` (F and B)."""
    from httpx import ASGITransport, AsyncClient

    from course_supporter.api.app import app
    from course_supporter.api.deps import get_current_student
    from course_supporter.auth.context import StudentContext
    from course_supporter.storage.database import get_session

    w = await make_world(session_factory)
    await buy(session_factory, w.student, 70)
    port = funds.LedgerFundsPort(session_factory)
    await port.check_and_reserve(review(w), 1.0)
    await call(session_factory, w.homework_job_id, "0.10")
    await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_student] = lambda: StudentContext(
        student_id=w.student_id, tenant_id=w.tenant_id, login="s", display_name=None
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"/api/v1/portal/submissions/{w.submission_id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["billing_note"] == {
        "kind": "cost_over_reserve",
        "cost_units": 90,
        "charged_units": 70,
    }
