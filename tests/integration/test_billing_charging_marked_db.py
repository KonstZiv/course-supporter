"""BIL-SCOPE on a live database: charging for marked accounts only.

Operator 2026-11-17 ("1b"): with ``BILLING_CHARGING_ENABLED`` off and
``BILLING_CHARGING_MARKED`` on, a payer whose account is marked
``test_purchase_allowed`` is charged as by today's charging; any other payer —
unmarked, or without an account — runs as with charging off: nothing reserved,
refused or held, no account created, nothing in ``billing_audit_log``. In a
course where the author pays, the author's mark decides.

Each place that reads the switch (``billing/charging.py``: ``charging_possible``
before the payer is known, ``charging_applies`` once it is) is checked both
ways. Numbers as in ``test_billing_charging_db``: K = 900, margin 0.20,
E = $0.05 → P = 54, 2E = 90; a call of $0.04 is F = 36.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_arq_redis,
    get_current_student,
    get_current_tenant,
    get_s3_client,
)
from course_supporter.api.routes.portal_submissions import _require_units
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.billing import admin, funds, ledger, periodic, quotes
from course_supporter.billing.accounts import (
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.kinds import ActionKind, MovementKind, RuleScope
from course_supporter.billing.resume_funded import resume_funded
from course_supporter.call_outcome import FundsDecision
from course_supporter.config import get_settings
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
from course_supporter.periodic.runner import TaskContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingAccount,
    BillingAuditEntry,
    BillingMovement,
    BillingReservation,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    StudentEnrollment,
    Tenant,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

E = Decimal("0.05")
P = 54
TWO_E = 90
F = 36
ACTOR = "test-admin-bil-scope"


class Mark(StrEnum):
    """The payer's account: marked, unmarked, or not created yet."""

    MARKED = "marked"
    UNMARKED = "unmarked"
    NO_ACCOUNT = "no_account"


NOT_CHARGED = [Mark.UNMARKED, Mark.NO_ACCOUNT]


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    course_id: uuid.UUID
    task_id: uuid.UUID
    student_id: uuid.UUID
    submission_id: uuid.UUID
    homework_job_id: uuid.UUID
    processing_job_id: uuid.UUID


_WORLDS: list[uuid.UUID] = []


@pytest.fixture(autouse=True)
async def _drop_billing(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    yield
    while _WORLDS:
        await drop_billing_of_tenant(session_factory, _WORLDS.pop())


def switches(monkeypatch: pytest.MonkeyPatch, *, enabled: bool, marked: bool) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_charging_enabled", enabled)
    monkeypatch.setattr(settings, "billing_charging_marked", marked)


@pytest.fixture(autouse=True)
def _marked_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mode under test; the path's test mode off; every action priced at E."""
    switches(monkeypatch, enabled=False, marked=True)
    monkeypatch.setattr(funds, "in_test_mode", lambda *a, **k: False)
    monkeypatch.setattr(quotes, "in_test_mode", lambda *a, **k: False)
    monkeypatch.setattr(funds, "processing_e_usd", lambda **_: E)
    monkeypatch.setattr(funds, "preparation_e_usd", lambda **_: E)


# ── The world ─────────────────────────────────────────────────────────


async def _account(
    session: AsyncSession,
    mark: Mark,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID | None,
) -> None:
    if mark is Mark.MARKED:
        await admin.set_test_purchase(
            session,
            tenant_id=tenant_id,
            student_id=student_id,
            allowed=True,
            actor=ACTOR,
        )
    elif mark is Mark.UNMARKED and student_id is not None:
        await get_or_create_student_account(
            session, tenant_id=tenant_id, student_id=student_id
        )
    elif mark is Mark.UNMARKED:
        await get_or_create_author_account(session, tenant_id=tenant_id)


async def make_world(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    student: Mark = Mark.NO_ACCOUNT,
    author: Mark = Mark.NO_ACCOUNT,
    author_pays: bool = False,
) -> World:
    """A tenant with a course, a task, an enrolled student and their submission;
    the accounts as asked (``author_pays``: a course rule for reviews)."""
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-scope-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        course = make_root_course_node(tenant_id=tenant.id, title="Курс", order=0)
        session.add(course)
        await session.flush()
        task = AuthoredDocument(
            course_node_id=course.id,
            course_root_id=course.id,
            source_type="text",
            source_url=f"https://example.com/{uuid.uuid4().hex}",
            filename="task.txt",
            task_type="task",
            language="ukr",
            content_hash=uuid.uuid4().hex * 2,
        )
        person = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:8]}")
        session.add_all([task, person])
        await session.flush()
        session.add(StudentEnrollment(student_id=person.id, course_node_id=course.id))
        submission = HomeworkSubmission(
            tenant_id=tenant.id,
            student_id=person.id,
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
        await _account(session, student, tenant_id=tenant.id, student_id=person.id)
        await _account(session, author, tenant_id=tenant.id, student_id=None)
        if author_pays:
            await admin.create_payment_rule(
                session,
                actor=ACTOR,
                tenant_id=tenant.id,
                scope=RuleScope.COURSE,
                action_kind=ActionKind.REVIEW,
                course_node_id=course.id,
            )
        await session.commit()
        _WORLDS.append(tenant.id)
        return World(
            tenant.id,
            course.id,
            task.id,
            person.id,
            submission.id,
            homework_job.id,
            processing_job.id,
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


async def account_of(
    session_factory: async_sessionmaker[AsyncSession],
    w: World,
    *,
    student: bool,
) -> uuid.UUID | None:
    owner = (
        BillingAccount.student_id == w.student_id
        if student
        else BillingAccount.student_id.is_(None)
    )
    async with session_factory() as session:
        found: uuid.UUID | None = await session.scalar(
            select(BillingAccount.id).where(
                BillingAccount.tenant_id == w.tenant_id, owner
            )
        )
    return found


async def buy(
    session_factory: async_sessionmaker[AsyncSession],
    w: World,
    units: int,
    *,
    student: bool,
) -> uuid.UUID:
    """Credit ``units`` to an existing account of the world; its id."""
    account = await account_of(session_factory, w, student=student)
    assert account is not None
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
    return account


async def call(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID, usd: str
) -> None:
    """One priced call of the job in the register."""
    async with session_factory() as session:
        session.add(
            ExternalServiceCall(
                job_id=job_id,
                action="criteria_evaluation",
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


@dataclass(frozen=True)
class Footprint:
    """What billing holds for a tenant, and the audit log as a whole."""

    accounts: int
    audit_entries: int
    movements: int
    reservations: int


async def footprint(
    session_factory: async_sessionmaker[AsyncSession], w: World
) -> Footprint:
    async with session_factory() as session:
        accounts = select(BillingAccount.id).where(
            BillingAccount.tenant_id == w.tenant_id
        )

        async def count(stmt: Any) -> int:
            return int(await session.scalar(stmt) or 0)

        return Footprint(
            accounts=await count(select(func.count()).select_from(accounts.subquery())),
            audit_entries=await count(select(func.count(BillingAuditEntry.id))),
            movements=await count(
                select(func.count(BillingMovement.id)).where(
                    BillingMovement.account_id.in_(accounts)
                )
            ),
            reservations=await count(
                select(func.count(BillingReservation.id)).where(
                    BillingReservation.account_id.in_(accounts)
                )
            ),
        )


async def movements(
    session_factory: async_sessionmaker[AsyncSession], kind: ActionKind, key: str
) -> dict[tuple[uuid.UUID, str], int]:
    """``{(account, movement kind): units}`` of one action."""
    async with session_factory() as session:
        rows = await session.execute(
            select(
                BillingMovement.account_id,
                BillingMovement.kind,
                func.sum(BillingMovement.units),
            )
            .where(
                BillingMovement.action_kind == kind.value,
                BillingMovement.action_key == key,
            )
            .group_by(BillingMovement.account_id, BillingMovement.kind)
        )
        return {(a, k): int(u) for a, k, u in rows.tuples()}


def review(w: World) -> SubmissionContext:
    return SubmissionContext(
        tenant_id=w.tenant_id,
        student_id=w.student_id,
        submission_id=w.submission_id,
        path_key=PathKey(AssignmentType.TASK, SubmissionState.FIRST),
        expected_usd=E,
    )


def version_work(w: World) -> VersionWorkContext:
    return VersionWorkContext(
        tenant_id=w.tenant_id,
        authored_document_id=w.task_id,
        source_content_hash="hash",
        work_kind=VersionWorkKind.KEY_EXPLANATION,
        reference_id=uuid.uuid4(),
        requested_by_submission=False,
        expected_usd=E,
    )


def version_key(w: World, work: VersionWorkContext) -> str:
    return f"{w.task_id}:{work.reference_id}:key_explanation"


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


async def end_processing(
    session_factory: async_sessionmaker[AsyncSession], w: World
) -> None:
    await funds.job_ended(
        session_factory,
        job_id=w.processing_job_id,
        authored_document_id=w.task_id,
        completed=True,
    )


async def start_preparation(
    session_factory: async_sessionmaker[AsyncSession], w: World
) -> funds.PreparationCharge | None:
    async with session_factory() as session:
        task = await session.get(AuthoredDocument, w.task_id)
    assert task is not None
    return await funds.preparation_started(
        session_factory,
        document=task,
        kind="criteria_decomposition",
        version="content-hash",
        task_chars=1000,
    )


def student_of(w: World) -> StudentContext:
    return StudentContext(
        student_id=w.student_id, tenant_id=w.tenant_id, login="s", display_name=None
    )


async def gate(session_factory: async_sessionmaker[AsyncSession], w: World) -> None:
    """The doors of a submission (``_require_units``) at the typical price."""
    async with session_factory() as session:
        task = await session.get(AuthoredDocument, w.task_id)
        assert task is not None
        await _require_units(session, task, student_of(w), file=None)
        await session.commit()


async def hold(session_factory: async_sessionmaker[AsyncSession], w: World) -> None:
    """The submission waits in ``awaiting_funds``; its job is over."""
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


async def resumed(session_factory: async_sessionmaker[AsyncSession], w: World) -> bool:
    """Would the continuation of held reviews (a dry run) continue the world's?"""
    async with session_factory() as session:
        done = await resume_funded(session, AsyncMock(), dry_run=True)
    return w.submission_id in done.resumed


@pytest.fixture()
def http(session_factory: async_sessionmaker[AsyncSession]) -> Iterator[None]:
    """The app on the test database; the caller signs in with :func:`sign_in`."""

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_s3_client] = lambda: MagicMock()
    app.dependency_overrides[get_arq_redis] = lambda: AsyncMock()
    yield
    app.dependency_overrides.clear()


def sign_in(w: World) -> AsyncClient:
    app.dependency_overrides[get_current_student] = lambda: student_of(w)
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
        tenant_id=w.tenant_id,
        tenant_name="t",
        scopes=["prep"],
        plan_id="basic",
        key_prefix="cs_test",
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── № 1: the choice of the port (charging_possible) ───────────────────


@pytest.mark.parametrize(
    ("enabled", "marked", "port"),
    [
        (False, False, AlwaysEnoughFundsPort),
        (False, True, funds.LedgerFundsPort),
        (True, False, funds.LedgerFundsPort),
        (True, True, funds.LedgerFundsPort),
    ],
)
def test_1_the_port_is_the_journals_while_charging_is_possible(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    marked: bool,
    port: type,
) -> None:
    switches(monkeypatch, enabled=enabled, marked=marked)
    assert type(build_funds_port(session_factory)) is port


# ── № 2: the start and end of a review (LedgerFundsPort) ──────────────


class Test2Review:
    async def test_marked_student_is_refused_short_then_reserved_and_charged(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, student=Mark.MARKED)
        account = await buy(session_factory, w, P - 1, student=True)
        port = funds.LedgerFundsPort(session_factory)

        short = await port.check_and_reserve(review(w), 1.0)
        await buy(session_factory, w, 500, student=True)
        started = await port.check_and_reserve(review(w), 1.0)
        await call(session_factory, w.homework_job_id, "0.04")
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        assert short.decision is FundsDecision.REFUSED
        assert started.decision is FundsDecision.ALLOWED
        moved = await movements(
            session_factory, ActionKind.REVIEW, str(w.submission_id)
        )
        assert moved[(account, MovementKind.RESERVE.value)] == TWO_E
        assert moved[(account, MovementKind.DEBIT.value)] == F

    @pytest.mark.parametrize("mark", NOT_CHARGED)
    async def test_not_marked_is_allowed_with_nothing_and_nothing_written(
        self, session_factory: async_sessionmaker[AsyncSession], mark: Mark
    ) -> None:
        w = await make_world(session_factory, student=mark)
        before = await footprint(session_factory, w)
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w), 1.0)  # 0 units
        await call(session_factory, w.homework_job_id, "0.04")
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)
        await port.release_remainder(review(w), SubmissionOutcome.FAILED)

        assert answer.decision is FundsDecision.ALLOWED
        assert await footprint(session_factory, w) == before


class Test2ReviewTheAuthorPays:
    async def test_marked_author_unmarked_student_the_author_is_charged(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(
            session_factory,
            student=Mark.UNMARKED,
            author=Mark.MARKED,
            author_pays=True,
        )
        port = funds.LedgerFundsPort(session_factory)

        held = await port.check_and_reserve(review(w), 1.0)  # the author has 0
        author = await buy(session_factory, w, 500, student=False)
        started = await port.check_and_reserve(review(w), 1.0)
        await call(session_factory, w.homework_job_id, "0.04")
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        assert held.decision is FundsDecision.REFUSED, "held for the author's units"
        assert started.decision is FundsDecision.ALLOWED
        moved = await movements(
            session_factory, ActionKind.REVIEW, str(w.submission_id)
        )
        assert moved[(author, MovementKind.DEBIT.value)] == F
        assert all(account == author for account, _ in moved), "not the student's"

    async def test_unmarked_author_marked_student_nothing_is_charged(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(
            session_factory,
            student=Mark.MARKED,
            author=Mark.UNMARKED,
            author_pays=True,
        )
        await buy(session_factory, w, 500, student=True)
        before = await footprint(session_factory, w)
        port = funds.LedgerFundsPort(session_factory)

        answer = await port.check_and_reserve(review(w), 1.0)  # the author has 0
        await call(session_factory, w.homework_job_id, "0.04")
        await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)

        assert answer.decision is FundsDecision.ALLOWED
        assert await footprint(session_factory, w) == before


# ── № 3: the work on a task's version (key explanations) ──────────────


class Test3VersionWork:
    async def test_marked_author_is_refused_short_then_reserved_and_charged(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, author=Mark.MARKED)
        port = funds.LedgerFundsPort(session_factory)
        work = version_work(w)

        short = await port.check_and_reserve_for_version(work, 0.08)
        author = await buy(session_factory, w, 500, student=False)
        started = await port.check_and_reserve_for_version(work, 0.08)
        await port.account_version_work_cost(work, 0.04)

        assert short.decision is FundsDecision.REFUSED
        assert started.decision is FundsDecision.ALLOWED
        moved = await movements(
            session_factory, ActionKind.TASK_PREPARATION, version_key(w, work)
        )
        assert moved[(author, MovementKind.RESERVE.value)] == 72, "the ceiling 0.08"
        assert moved[(author, MovementKind.DEBIT.value)] == F

    @pytest.mark.parametrize("mark", NOT_CHARGED)
    async def test_not_marked_is_allowed_with_nothing_and_nothing_written(
        self, session_factory: async_sessionmaker[AsyncSession], mark: Mark
    ) -> None:
        w = await make_world(session_factory, author=mark)
        before = await footprint(session_factory, w)
        port = funds.LedgerFundsPort(session_factory)
        work = version_work(w)

        answer = await port.check_and_reserve_for_version(work, 0.08)
        await port.account_version_work_cost(work, 0.04)

        assert answer.decision is FundsDecision.ALLOWED
        assert await footprint(session_factory, w) == before


# ── № 4, № 5: the processing of a material, its start and its end ─────


class Test4ProcessingStart:
    async def test_marked_author_short_fails_the_job_covered_reserves(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, author=Mark.MARKED)
        key = str(w.processing_job_id)

        with pytest.raises(funds.InsufficientUnitsError):
            await start_processing(session_factory, w)
        author = await buy(session_factory, w, 500, student=False)
        await start_processing(session_factory, w)

        moved = await movements(session_factory, ActionKind.PROCESSING, key)
        assert moved == {(author, MovementKind.RESERVE.value): TWO_E}
        await funds.job_ended(
            session_factory,
            job_id=w.processing_job_id,
            authored_document_id=w.task_id,
            completed=False,
        )

    @pytest.mark.parametrize("mark", NOT_CHARGED)
    async def test_not_marked_starts_with_nothing_and_nothing_written(
        self, session_factory: async_sessionmaker[AsyncSession], mark: Mark
    ) -> None:
        w = await make_world(session_factory, author=mark)
        before = await footprint(session_factory, w)

        await start_processing(session_factory, w)  # 0 units: no refusal

        assert await footprint(session_factory, w) == before


class Test5ProcessingEnd:
    async def test_marked_author_is_charged_the_fact(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, author=Mark.MARKED)
        author = await buy(session_factory, w, 500, student=False)
        await start_processing(session_factory, w)
        await call(session_factory, w.processing_job_id, "0.04")

        await end_processing(session_factory, w)

        moved = await movements(
            session_factory, ActionKind.PROCESSING, str(w.processing_job_id)
        )
        assert moved[(author, MovementKind.DEBIT.value)] == F
        assert moved[(author, MovementKind.RESERVE_RELEASE.value)] == TWO_E - F

    @pytest.mark.parametrize("mark", NOT_CHARGED)
    async def test_not_marked_end_writes_nothing(
        self, session_factory: async_sessionmaker[AsyncSession], mark: Mark
    ) -> None:
        w = await make_world(session_factory, author=mark)
        before = await footprint(session_factory, w)
        await start_processing(session_factory, w)
        await call(session_factory, w.processing_job_id, "0.04")

        await end_processing(session_factory, w)

        assert await footprint(session_factory, w) == before


# ── № 6: the preparation a submission starts (criteria decomposition) ─


class Test6Preparation:
    async def test_marked_author_reserves_and_is_charged(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, author=Mark.MARKED)
        author = await buy(session_factory, w, 500, student=False)

        charge = await start_preparation(session_factory, w)
        assert charge is not None
        await funds.preparation_ended(session_factory, charge, fact_usd=Decimal("0.04"))

        moved = await movements(
            session_factory, ActionKind.TASK_PREPARATION, charge.action_key
        )
        assert moved[(author, MovementKind.RESERVE.value)] == TWO_E
        assert moved[(author, MovementKind.DEBIT.value)] == F

    @pytest.mark.parametrize("mark", NOT_CHARGED)
    async def test_not_marked_reserves_nothing_and_writes_nothing(
        self, session_factory: async_sessionmaker[AsyncSession], mark: Mark
    ) -> None:
        w = await make_world(session_factory, author=mark)
        before = await footprint(session_factory, w)

        charge = await start_preparation(session_factory, w)
        await funds.preparation_ended(session_factory, charge, fact_usd=Decimal("0.04"))

        assert charge is None
        assert await footprint(session_factory, w) == before


# ── № 7: the doors of a submission ────────────────────────────────────


class Test7Doors:
    async def test_marked_student_short_is_refused_with_402(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, student=Mark.MARKED)

        with pytest.raises(HTTPException) as refused:
            await gate(session_factory, w)

        assert refused.value.status_code == 402
        detail: Any = refused.value.detail
        assert detail["code"] == "insufficient_units"

    @pytest.mark.parametrize("mark", NOT_CHARGED)
    async def test_not_marked_short_is_taken_and_nothing_written(
        self, session_factory: async_sessionmaker[AsyncSession], mark: Mark
    ) -> None:
        w = await make_world(session_factory, student=mark)
        before = await footprint(session_factory, w)

        await gate(session_factory, w)  # 0 units: taken

        assert await footprint(session_factory, w) == before

    async def test_unmarked_author_paying_for_a_marked_student_is_taken(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(
            session_factory,
            student=Mark.MARKED,
            author=Mark.UNMARKED,
            author_pays=True,
        )
        await gate(session_factory, w)  # the author pays: never refused here


# ── № 8: the price of a student's review ──────────────────────────────


async def review_quote(
    session_factory: async_sessionmaker[AsyncSession], w: World
) -> quotes.Quote:
    async with session_factory() as session:
        task = await session.get(AuthoredDocument, w.task_id)
        assert task is not None
        return await quotes.review_quote(
            session, task=task, tenant_id=w.tenant_id, student_id=w.student_id
        )


class Test8ReviewQuote:
    async def test_marked_student_charging_on_short_not_enough(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, student=Mark.MARKED)

        quote = await review_quote(session_factory, w)

        assert quote.charging_enabled is True
        assert quote.enough is False
        assert quote.price_units is not None and quote.price_units > 0

    @pytest.mark.parametrize("mark", NOT_CHARGED)
    async def test_not_marked_charging_off_enough_nothing_written(
        self, session_factory: async_sessionmaker[AsyncSession], mark: Mark
    ) -> None:
        w = await make_world(session_factory, student=mark)
        before = await footprint(session_factory, w)

        quote = await review_quote(session_factory, w)

        assert (quote.charging_enabled, quote.enough, quote.missing_units) == (
            False,
            True,
            0,
        )
        assert await footprint(session_factory, w) == before

    @pytest.mark.parametrize(
        ("student", "author", "enabled"),
        [
            (Mark.UNMARKED, Mark.MARKED, True),
            (Mark.MARKED, Mark.UNMARKED, False),
        ],
    )
    async def test_the_author_pays_the_authors_mark_decides(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        student: Mark,
        author: Mark,
        enabled: bool,
    ) -> None:
        w = await make_world(
            session_factory, student=student, author=author, author_pays=True
        )

        quote = await review_quote(session_factory, w)

        assert (quote.paid_by, quote.charging_enabled) == ("author", enabled)


# ── № 9: the price of an author's action ──────────────────────────────


async def author_quote(
    session_factory: async_sessionmaker[AsyncSession], w: World
) -> quotes.Quote:
    async with session_factory() as session:
        return await quotes.author_quote(
            session,
            action_kind=ActionKind.PROCESSING,
            tenant_id=w.tenant_id,
            course_node_id=w.course_id,
            e_usd=E,
        )


class Test9AuthorQuote:
    async def test_marked_author_charging_on_short_not_enough(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, author=Mark.MARKED)

        quote = await author_quote(session_factory, w)

        assert (quote.charging_enabled, quote.enough, quote.missing_units) == (
            True,
            False,
            P,
        )

    @pytest.mark.parametrize("mark", NOT_CHARGED)
    async def test_not_marked_charging_off_enough_nothing_written(
        self, session_factory: async_sessionmaker[AsyncSession], mark: Mark
    ) -> None:
        w = await make_world(session_factory, author=mark)
        before = await footprint(session_factory, w)

        quote = await author_quote(session_factory, w)

        assert (quote.charging_enabled, quote.enough, quote.missing_units) == (
            False,
            True,
            0,
        )
        assert await footprint(session_factory, w) == before


# ── № 10, № 11: the reminders and the spending ────────────────────────


@pytest.mark.usefixtures("http")
class Test10Outlooks:
    @pytest.mark.parametrize(
        ("mark", "enabled"),
        [(Mark.MARKED, True), (Mark.UNMARKED, False), (Mark.NO_ACCOUNT, False)],
    )
    async def test_the_students_says_whether_charging_applies_to_them(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mark: Mark,
        enabled: bool,
    ) -> None:
        w = await make_world(session_factory, student=mark)
        before = await footprint(session_factory, w)

        async with sign_in(w) as client:
            response = await client.get("/api/v1/portal/billing/outlook")

        assert response.status_code == 200, response.text
        assert response.json()["charging_enabled"] is enabled
        assert await footprint(session_factory, w) == before

    @pytest.mark.parametrize(
        ("student", "author", "enabled"),
        [
            (Mark.UNMARKED, Mark.MARKED, True),
            (Mark.MARKED, Mark.UNMARKED, False),
        ],
    )
    async def test_the_students_follows_the_author_paying_in_the_course(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        student: Mark,
        author: Mark,
        enabled: bool,
    ) -> None:
        w = await make_world(
            session_factory, student=student, author=author, author_pays=True
        )

        async with sign_in(w) as client:
            response = await client.get(
                "/api/v1/portal/billing/outlook", params={"course_id": str(w.course_id)}
            )

        assert response.status_code == 200, response.text
        assert (response.json()["paid_by"], response.json()["charging_enabled"]) == (
            "author",
            enabled,
        )

    @pytest.mark.parametrize(
        ("mark", "enabled"),
        [(Mark.MARKED, True), (Mark.UNMARKED, False), (Mark.NO_ACCOUNT, False)],
    )
    async def test_the_authors_says_whether_charging_applies_to_them(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mark: Mark,
        enabled: bool,
    ) -> None:
        w = await make_world(session_factory, author=mark)
        before = await footprint(session_factory, w)

        async with sign_in(w) as client:
            response = await client.get("/api/v1/billing/outlook")

        assert response.status_code == 200, response.text
        assert response.json()["charging_enabled"] is enabled
        assert await footprint(session_factory, w) == before


@pytest.mark.usefixtures("http")
class Test11Spending:
    @pytest.mark.parametrize(
        ("mark", "enabled"),
        [(Mark.MARKED, True), (Mark.UNMARKED, False), (Mark.NO_ACCOUNT, False)],
    )
    async def test_says_whether_charging_applies_to_the_author(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mark: Mark,
        enabled: bool,
    ) -> None:
        w = await make_world(session_factory, author=mark)
        before = await footprint(session_factory, w)

        async with sign_in(w) as client:
            response = await client.get("/api/v1/billing/spending")

        assert response.status_code == 200, response.text
        assert response.json()["charging_enabled"] is enabled
        assert await footprint(session_factory, w) == before


# ── № 12: the continuation of held reviews ────────────────────────────


@pytest.mark.parametrize(("marked", "skipped"), [(True, False), (False, True)])
async def test_12_the_continuation_runs_while_charging_is_possible(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    marked: bool,
    skipped: bool,
) -> None:
    switches(monkeypatch, enabled=False, marked=marked)
    run = TaskContext(
        task_name="billing_resume_funded",
        session_factory=session_factory,
        settings=get_settings(),
        now=datetime.now(UTC),
        dry_run=True,
        redis=AsyncMock(),
    )

    result = await periodic.BILLING_RESUME_FUNDED.body(run)

    assert ("skipped" in result.details) is skipped


class Test12HeldReviews:
    async def test_marked_student_is_continued_once_they_can_pay(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await make_world(session_factory, student=Mark.MARKED)
        await hold(session_factory, w)

        short = await resumed(session_factory, w)
        await buy(session_factory, w, 10_000, student=True)
        funded = await resumed(session_factory, w)

        assert (short, funded) == (False, True)

    @pytest.mark.parametrize("mark", NOT_CHARGED)
    async def test_not_marked_without_units_nothing_written(
        self, session_factory: async_sessionmaker[AsyncSession], mark: Mark
    ) -> None:
        w = await make_world(session_factory, student=mark)
        await hold(session_factory, w)
        before = await footprint(session_factory, w)

        assert await resumed(session_factory, w) is False
        assert await footprint(session_factory, w) == before


# ── Regression: both switches off; charging for every account ─────────


async def test_both_off_nothing_changes_for_a_marked_payer(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switches(monkeypatch, enabled=False, marked=False)
    w = await make_world(session_factory, student=Mark.MARKED, author=Mark.MARKED)
    before = await footprint(session_factory, w)
    port = build_funds_port(session_factory)

    answer = await port.check_and_reserve(review(w), 1.0)
    await gate(session_factory, w)
    await start_processing(session_factory, w)
    await end_processing(session_factory, w)
    charge = await start_preparation(session_factory, w)
    quote = await review_quote(session_factory, w)
    own = await author_quote(session_factory, w)

    assert isinstance(port, AlwaysEnoughFundsPort)
    assert answer.decision is FundsDecision.ALLOWED
    assert charge is None
    assert (quote.charging_enabled, quote.enough) == (False, True)
    assert (own.charging_enabled, own.enough) == (False, True)
    assert await footprint(session_factory, w) == before


@pytest.mark.parametrize("marked", [False, True])
async def test_enabled_charges_every_payer_whatever_the_mark(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    marked: bool,
) -> None:
    switches(monkeypatch, enabled=True, marked=marked)
    w = await make_world(session_factory, student=Mark.UNMARKED, author=Mark.UNMARKED)
    student = await buy(session_factory, w, 500, student=True)
    port = build_funds_port(session_factory)

    answer = await port.check_and_reserve(review(w), 1.0)
    with pytest.raises(funds.InsufficientUnitsError):
        await start_processing(session_factory, w)  # the author has 0
    quote = await author_quote(session_factory, w)

    assert isinstance(port, funds.LedgerFundsPort)
    assert answer.decision is FundsDecision.ALLOWED
    moved = await movements(session_factory, ActionKind.REVIEW, str(w.submission_id))
    assert moved == {(student, MovementKind.RESERVE.value): TWO_E}
    assert (quote.charging_enabled, quote.enough) == (True, False)
    await port.release_remainder(review(w), SubmissionOutcome.FAILED)


# ── End to end: a student's review from the doors to the statement ────


@pytest.mark.usefixtures("http")
async def test_end_to_end_a_marked_students_review_reaches_the_statement(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    w = await make_world(session_factory, student=Mark.MARKED)
    account = await buy(session_factory, w, 500, student=True)

    await gate(session_factory, w)
    port = build_funds_port(session_factory)
    answer = await port.check_and_reserve(review(w), 1.0)
    await call(session_factory, w.homework_job_id, "0.04")
    await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)
    async with sign_in(w) as client:
        response = await client.get("/api/v1/portal/billing/statement")

    assert answer.decision is FundsDecision.ALLOWED
    moved = await movements(session_factory, ActionKind.REVIEW, str(w.submission_id))
    assert moved == {
        (account, MovementKind.RESERVE.value): TWO_E,
        (account, MovementKind.DEBIT.value): F,
        (account, MovementKind.RESERVE_RELEASE.value): TWO_E - F,
    }
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    debit = [item for item in items if item["kind"] == "charge"]
    assert [(line["units"], line["balance_after"]) for line in debit] == [(-F, 500 - F)]
    async with session_factory() as session:
        balance = await ledger.balance(session, account)
    assert (balance.balance, balance.available) == (500 - F, 500 - F)


@pytest.mark.usefixtures("http")
@pytest.mark.parametrize("mark", NOT_CHARGED)
async def test_end_to_end_an_unmarked_students_review_leaves_no_billing_row(
    session_factory: async_sessionmaker[AsyncSession], mark: Mark
) -> None:
    w = await make_world(session_factory, student=mark)
    before = await footprint(session_factory, w)

    await gate(session_factory, w)
    port = build_funds_port(session_factory)
    answer = await port.check_and_reserve(review(w), 1.0)
    await call(session_factory, w.homework_job_id, "0.04")
    await port.release_remainder(review(w), SubmissionOutcome.COMPLETED)
    async with sign_in(w) as client:
        response = await client.get("/api/v1/portal/billing/statement")

    assert answer.decision is FundsDecision.ALLOWED
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert await footprint(session_factory, w) == before
