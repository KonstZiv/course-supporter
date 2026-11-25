"""BIL-COSTS Б-2: what the students paid for the reviews, in the cost report.

``costs`` gives, in the author and task rows and their totals, ``reviews``,
``units_per_review``, ``students_money_minor`` and ``gift_units``, and leaves
the service students (``students.is_test_student``) out unless
``--include-test-students``. The journal is written by the ledger itself
(``credit_purchase`` / ``credit_admin`` / ``settle``) at fixed instants in
October 2026, so the period is exact whatever day the test runs on; batches
are taken in the order they expire, so a batch bought earlier pays first.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from scripts import billing_admin
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.billing import costs, ledger, testmark
from course_supporter.billing.accounts import (
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.kinds import ActionKind, MarkOrigin, PayerSource
from course_supporter.cost_kinds import CostKind
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingAccount,
    BillingBatch,
    BillingTestBatch,
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

FROM = datetime(2026, 10, 1, tzinfo=UTC).date()
TO = datetime(2026, 10, 31, tzinfo=UTC).date()
IN_OCTOBER = datetime(2026, 10, 15, 12, tzinfo=UTC)
AFTER_OCTOBER = datetime(2026, 11, 2, 12, tzinfo=UTC)
DAY_1 = datetime(2026, 9, 1, tzinfo=UTC)
DAY_2 = datetime(2026, 9, 2, tzinfo=UTC)
DAY_3 = datetime(2026, 9, 3, tzinfo=UTC)

Factory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Learner:
    student_id: uuid.UUID
    account_id: uuid.UUID


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    tenant_name: str
    course: uuid.UUID
    task_a: uuid.UUID
    task_b: uuid.UUID
    author_account: uuid.UUID
    learners: dict[str, Learner]


async def _make_world(factory: Factory, learners: dict[str, bool]) -> World:
    """One tenant, one course with two tasks, a student account for each name
    (the value: is the student a service one)."""
    async with factory() as session:
        tenant = Tenant(name=f"bil-costs-b2-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        course = make_root_course_node(tenant_id=tenant.id, title="Курс", order=0)
        session.add(course)
        await session.flush()
        tasks = [
            AuthoredDocument(
                course_node_id=course.id,
                course_root_id=course.id,
                source_type="text",
                source_url=f"https://example.com/{uuid.uuid4().hex}",
                title=title,
                task_type="task",
            )
            for title in ("Завдання A", "Завдання B")
        ]
        session.add_all(tasks)
        students = {
            name: Student(
                tenant_id=tenant.id,
                external_id=f"{name}-{uuid.uuid4().hex[:8]}",
                is_test_student=service,
            )
            for name, service in learners.items()
        }
        session.add_all(students.values())
        await session.flush()
        author = await get_or_create_author_account(session, tenant_id=tenant.id)
        accounts = {
            name: await get_or_create_student_account(
                session, tenant_id=tenant.id, student_id=student.id
            )
            for name, student in students.items()
        }
        await session.commit()
        return World(
            tenant_id=tenant.id,
            tenant_name=tenant.name,
            course=course.id,
            task_a=tasks[0].id,
            task_b=tasks[1].id,
            author_account=author.id,
            learners={
                name: Learner(students[name].id, accounts[name].id) for name in students
            },
        )


async def _drop_world(factory: Factory, world: World) -> None:
    async with factory() as session:
        accounts = select(BillingAccount.id).where(
            BillingAccount.tenant_id == world.tenant_id
        )
        batches = select(BillingBatch.id).where(BillingBatch.account_id.in_(accounts))
        await session.execute(
            delete(BillingTestBatch).where(BillingTestBatch.batch_id.in_(batches))
        )
        jobs = select(Job.id).where(Job.tenant_id == world.tenant_id)
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id.in_(jobs))
        )
        await session.execute(delete(Job).where(Job.tenant_id == world.tenant_id))
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == world.tenant_id
            )
        )
        await session.commit()
    await drop_billing_of_tenant(factory, world.tenant_id)
    async with factory() as session:
        await session.execute(
            delete(AuthoredDocument).where(
                AuthoredDocument.course_root_id == world.course
            )
        )
        await session.execute(delete(CourseNode).where(CourseNode.id == world.course))
        await session.execute(
            delete(Student).where(Student.tenant_id == world.tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == world.tenant_id))
        await session.commit()


@pytest.fixture()
async def worlds(session_factory: Factory) -> AsyncGenerator[list[World]]:
    made: list[World] = []
    yield made
    for world in made:
        await _drop_world(session_factory, world)


async def _world(
    factory: Factory, worlds: list[World], learners: dict[str, bool]
) -> World:
    world = await _make_world(factory, learners)
    worlds.append(world)
    return world


async def _buy(
    factory: Factory,
    account: uuid.UUID,
    units: int,
    minor: int,
    currency: str,
    at: datetime,
    *,
    test_purchase: bool = False,
) -> None:
    """A purchase of a package; ``test_purchase`` marks it (BIL-TESTMARK)."""
    async with factory() as session:
        credit = await ledger.credit_purchase(
            session,
            account_id=account,
            units=units,
            amount_paid_minor=minor,
            currency=currency,
            provider="fake",
            now=at,
        )
        if test_purchase:
            await testmark.mark(
                session, batch_id=credit.batch.id, origin=MarkOrigin.CREDIT, now=at
            )
        await session.commit()


async def _gift(factory: Factory, account: uuid.UUID, units: int, at: datetime) -> None:
    async with factory() as session:
        await ledger.credit_admin(
            session,
            account_id=account,
            units=units,
            reason="pilot",
            actor="test",
            now=at,
        )
        await session.commit()


async def _review(
    factory: Factory,
    world: World,
    who: str,
    task: uuid.UUID,
    units: int,
    *,
    at: datetime = IN_OCTOBER,
    payer: uuid.UUID | None = None,
) -> uuid.UUID:
    """A review of the student's work on the task, settled to ``units``; the
    student's own account pays, or ``payer`` (the author under a rule)."""
    learner = world.learners[who]
    async with factory() as session:
        submission = HomeworkSubmission(
            tenant_id=world.tenant_id,
            student_id=learner.student_id,
            course_node_id=world.course,
            node_id=world.course,
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
            beneficiary_account_id=learner.account_id,
            payer_source=PayerSource.DEFAULT
            if payer is None
            else PayerSource.COURSE_RULE,
            account_id=payer or learner.account_id,
            now=at,
        )
        await session.commit()
        return submission.id


async def _report(
    factory: Factory,
    world: World,
    *,
    by: costs.CostReportScope = "task",
    include_test_students: bool = False,
) -> costs.CostReport:
    async with factory() as session:
        return await costs.build_cost_report(
            session,
            from_date=FROM,
            to_date=TO,
            tenant_id=world.tenant_id,
            by=by,
            include_test_students=include_test_students,
        )


def _task_row(report: costs.CostReport, task: uuid.UUID) -> costs.TaskCostRow:
    return next(r for r in report.tasks if r.authored_document_id == task)


# ── Brief lock a: the money of a paid debit ──────────────────────────────────────


async def test_a_paid_debit_is_worth_units_times_the_price_of_its_batch(
    session_factory: Factory, worlds: list[World]
) -> None:
    world = await _world(session_factory, worlds, {"alice": False})
    alice = world.learners["alice"]
    await _buy(session_factory, alice.account_id, 100, 1000, "EUR", DAY_1)
    await _review(session_factory, world, "alice", world.task_a, 30)
    # Outside the period: neither a review nor money of it.
    await _review(session_factory, world, "alice", world.task_a, 5, at=AFTER_OCTOBER)

    report = await _report(session_factory, world)

    row = _task_row(report, world.task_a)
    assert row.reviews == costs.ReviewFigures(
        count=1, units_per_review=30, money_minor=(("EUR", 300),), gift_units=0
    ), "30 units of a batch that sold 100 units for 1000: 30 x 10 = 300"
    assert row.units.students_units == 30
    assert report.tasks_total.reviews == row.reviews


async def test_a_review_over_two_batches_of_different_price_sums_the_shares(
    session_factory: Factory, worlds: list[World]
) -> None:
    world = await _world(session_factory, worlds, {"alice": False})
    alice = world.learners["alice"]
    await _buy(session_factory, alice.account_id, 60, 300, "EUR", DAY_1)  # 5 a unit
    await _buy(session_factory, alice.account_id, 100, 1500, "EUR", DAY_2)  # 15
    await _review(session_factory, world, "alice", world.task_a, 100)

    report = await _report(session_factory, world)

    row = _task_row(report, world.task_a)
    assert row.reviews.count == 1, "two debits, one review"
    assert row.reviews.money_minor == (("EUR", 60 * 5 + 40 * 15),)
    assert row.reviews.units_per_review == 100


async def test_the_money_is_rounded_once_per_row_half_up(
    session_factory: Factory, worlds: list[World]
) -> None:
    world = await _world(session_factory, worlds, {"alice": False, "bob": False})
    alice, bob = world.learners["alice"], world.learners["bob"]
    # 100 / 7 = 14.29 a unit: two reviews of one unit are 28.57, not 14 + 14.
    await _buy(session_factory, alice.account_id, 7, 100, "EUR", DAY_1)
    await _review(session_factory, world, "alice", world.task_a, 1)
    await _review(session_factory, world, "alice", world.task_a, 1)
    # 1 / 2 = 0.5 a unit: half goes up.
    await _buy(session_factory, bob.account_id, 2, 1, "EUR", DAY_1)
    await _review(session_factory, world, "bob", world.task_b, 1)

    report = await _report(session_factory, world)

    assert _task_row(report, world.task_a).reviews.money_minor == (("EUR", 29),)
    assert _task_row(report, world.task_b).reviews.money_minor == (("EUR", 1),)
    assert report.tasks_total.reviews.money_minor == (("EUR", 29),), (
        "the totals row is rounded on its own: 28.57 + 0.5 = 29.07, "
        "not the 29 + 1 of the rounded rows"
    )
    author = (await _report(session_factory, world, by="author")).authors[0]
    assert author.reviews.money_minor == (("EUR", 29),)


# ── Brief lock b: gifts and test purchases are no money ──────────────────────────


async def test_gift_and_test_batches_count_as_gift_units_not_money(
    session_factory: Factory, worlds: list[World]
) -> None:
    world = await _world(session_factory, worlds, {"alice": False})
    alice = world.learners["alice"]
    await _gift(session_factory, alice.account_id, 50, DAY_1)
    await _buy(
        session_factory, alice.account_id, 40, 400, "EUR", DAY_2, test_purchase=True
    )
    await _buy(session_factory, alice.account_id, 100, 1000, "EUR", DAY_3)
    await _review(session_factory, world, "alice", world.task_a, 90)  # gift + test
    await _review(session_factory, world, "alice", world.task_a, 10)  # the paid one

    report = await _report(session_factory, world)

    figures = _task_row(report, world.task_a).reviews
    assert figures.gift_units == 90, "50 of an admin credit + 40 of a test purchase"
    assert figures.money_minor == (("EUR", 100),), (
        "only the 10 units of the paid batch are money; the marked batch's 400 is not"
    )
    assert figures.count == 2
    assert figures.units_per_review == 50


async def test_a_review_the_author_paid_is_not_the_students(
    session_factory: Factory, worlds: list[World]
) -> None:
    world = await _world(session_factory, worlds, {"alice": False})
    alice = world.learners["alice"]
    await _buy(session_factory, world.author_account, 100, 1000, "EUR", DAY_1)
    await _buy(session_factory, alice.account_id, 100, 1000, "EUR", DAY_1)
    await _review(
        session_factory, world, "alice", world.task_a, 40, payer=world.author_account
    )
    await _review(session_factory, world, "alice", world.task_a, 20)

    report = await _report(session_factory, world)

    row = _task_row(report, world.task_a)
    assert row.units.author_units == 40
    assert row.reviews == costs.ReviewFigures(
        count=1, units_per_review=20, money_minor=(("EUR", 200),), gift_units=0
    ), "the author's own 40 units are neither a review of the students nor money"


# ── Brief lock v: the service students ───────────────────────────────────────────


async def _seed_service_student(
    factory: Factory, worlds: list[World]
) -> tuple[World, uuid.UUID]:
    """Alice (10 units) and a service student (20 units), a paid unit costing
    10 for both; the service student's review has a paid call of 0.25 dollars.
    Returns the world and the service student's submission."""
    world = await _world(factory, worlds, {"alice": False, "svc": True})
    for who in ("alice", "svc"):
        await _buy(factory, world.learners[who].account_id, 100, 1000, "EUR", DAY_1)
    await _review(factory, world, "alice", world.task_a, 10)
    submission = await _review(factory, world, "svc", world.task_a, 20)
    async with factory() as session:
        job = Job(
            tenant_id=world.tenant_id,
            course_node_id=world.course,
            job_type=JobType.HOMEWORK_PROCESSING.value,
            subject_type=JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING],
            subject_id=submission,
            status="complete",
        )
        session.add(job)
        await session.flush()
        session.add(
            ExternalServiceCall(
                job_id=job.id,
                action="criteria_evaluation",
                provider="gemini",
                model_id="gemini-2.5-flash",
                success=True,
                unit_type="tokens",
                unit_in=1,
                unit_out=1,
                cost_usd=Decimal("0.25"),
                cost_kind=CostKind.ESTIMATED.value,
                call_started_at=IN_OCTOBER,
                created_at=IN_OCTOBER,
            )
        )
        await session.commit()
    return world, submission


async def test_test_students_are_left_out_unless_included(
    session_factory: Factory, worlds: list[World]
) -> None:
    world, _ = await _seed_service_student(session_factory, worlds)

    left_out = await _report(session_factory, world)
    included = await _report(session_factory, world, include_test_students=True)

    row = _task_row(left_out, world.task_a)
    assert row.reviews == costs.ReviewFigures(
        count=1, units_per_review=10, money_minor=(("EUR", 100),), gift_units=0
    )
    assert row.units.students_units == 10, "the existing column leaves them out too"
    assert left_out.excluded_test_reviews == 1

    row = _task_row(included, world.task_a)
    assert row.reviews == costs.ReviewFigures(
        count=2, units_per_review=15, money_minor=(("EUR", 300),), gift_units=0
    )
    assert row.units.students_units == 30
    assert included.excluded_test_reviews == 0


async def test_test_students_are_left_out_of_the_students_units_too(
    session_factory: Factory, worlds: list[World]
) -> None:
    world, _ = await _seed_service_student(session_factory, worlds)

    for include, expected in ((False, 10), (True, 30)):
        report = await _report(
            session_factory, world, by="both", include_test_students=include
        )
        assert report.authors[0].units.students_units == expected
        assert report.authors_total.reviews.count == (1 if not include else 2)
        assert sum(r.units.students_units for r in report.students) == expected, (
            "the students' table agrees with the author's"
        )


async def test_the_dollars_do_not_depend_on_the_service_mark(
    session_factory: Factory, worlds: list[World]
) -> None:
    world, _ = await _seed_service_student(session_factory, worlds)

    left_out = await _report(session_factory, world, by="both")
    included = await _report(
        session_factory, world, by="both", include_test_students=True
    )

    assert left_out.authors[0].spend == included.authors[0].spend
    assert left_out.authors[0].spend.known_usd == Decimal("0.25")
    assert [(s.student_id, s.spend) for s in left_out.students] == [
        (s.student_id, s.spend) for s in included.students
    ], "the service student's row keeps its dollars"


async def test_the_excluded_line_and_the_flag_on_the_command_line(
    session_factory: Factory, worlds: list[World]
) -> None:
    world, _ = await _seed_service_student(session_factory, worlds)
    base = [
        "costs",
        "--tenant",
        str(world.tenant_id),
        "--from",
        FROM.isoformat(),
        "--to",
        TO.isoformat(),
    ]

    async def run(*extra: str) -> str:
        args = billing_admin.build_parser().parse_args([*base, *extra])
        async with session_factory() as session:
            return await billing_admin.costs_cmd(session, args)

    for by in ("author", "task", "student"):
        table = await run("--by", by)
        assert "excluded: 1 reviews of test students" in table.splitlines(), by
        table = await run("--by", by, "--include-test-students")
        assert (
            "excluded: 0 reviews of test students (--include-test-students)"
            in table.splitlines()
        ), by

    csv_rows = list(
        csv.DictReader(io.StringIO(await run("--by", "task", "--format", "csv")))
    )
    total = next(r for r in csv_rows if r["row_kind"] == "total")
    assert total["excluded_test_reviews"] == "1"
    assert total["reviews"] == "1"
    csv_rows = list(
        csv.DictReader(
            io.StringIO(
                await run("--by", "task", "--format", "csv", "--include-test-students")
            )
        )
    )
    total = next(r for r in csv_rows if r["row_kind"] == "total")
    assert (total["excluded_test_reviews"], total["reviews"]) == ("0", "2")


# ── Brief lock g: reviews and units per review ───────────────────────────────────


async def test_reviews_and_units_per_review_on_two_reviews_of_different_price(
    session_factory: Factory, worlds: list[World]
) -> None:
    world = await _world(session_factory, worlds, {"alice": False})
    alice = world.learners["alice"]
    await _buy(session_factory, alice.account_id, 500, 5000, "EUR", DAY_1)
    await _review(session_factory, world, "alice", world.task_a, 10)
    await _review(session_factory, world, "alice", world.task_a, 25)

    by_task = await _report(session_factory, world)
    by_author = await _report(session_factory, world, by="author")

    row = _task_row(by_task, world.task_a)
    assert (row.reviews.count, row.reviews.units_per_review) == (2, 18), (
        "(10 + 25) / 2 = 17.5, rounded up"
    )
    for figures in (by_task.tasks_total.reviews, by_author.authors[0].reviews):
        assert (figures.count, figures.units_per_review) == (2, 18)
        assert figures.money_minor == (("EUR", 350),)
    assert by_author.authors_total.reviews == by_author.authors[0].reviews

    table = billing_admin._render_cost_table(by_task, tenant_label=None)
    header = next(line for line in table.splitlines() if "units_per_review" in line)
    for name in ("reviews", "students_money_minor", "gift_units"):
        assert name in header
    assert "EUR 350" in table
    csv_rows = list(
        csv.DictReader(io.StringIO(billing_admin._render_cost_csv(by_task)))
    )
    total = next(r for r in csv_rows if r["row_kind"] == "total")
    assert (total["reviews"], total["units_per_review"]) == ("2", "18")
    assert (total["students_money_minor"], total["gift_units"]) == ("EUR 350", "0")


async def test_no_reviews_gives_a_dash(
    session_factory: Factory, worlds: list[World]
) -> None:
    world = await _world(session_factory, worlds, {"alice": False})
    await _buy(session_factory, world.author_account, 100, 1000, "EUR", DAY_1)
    async with session_factory() as session:
        await ledger.settle(
            session,
            action_kind=ActionKind.PROCESSING,
            action_key=str(uuid.uuid4()),
            fact_units=40,
            beneficiary_account_id=world.author_account,
            payer_source=PayerSource.DEFAULT,
            account_id=world.author_account,
            now=IN_OCTOBER,
        )
        await session.commit()

    by_author = await _report(session_factory, world, by="author")
    by_task = await _report(session_factory, world)

    for figures in (by_author.authors[0].reviews, by_task.tasks_total.reviews):
        assert figures == costs.ReviewFigures(), "no reviews: no average"
        assert figures.units_per_review is None
    for table in (
        billing_admin._render_cost_table(by_author, tenant_label=None),
        billing_admin._render_cost_table(by_task, tenant_label=None),
    ):
        assert billing_admin.NO_AVERAGE == "—"
        assert f" {billing_admin.NO_AVERAGE} " in table
    rows = list(csv.DictReader(io.StringIO(billing_admin._render_cost_csv(by_task))))
    assert {r["units_per_review"] for r in rows} == {"—"}


# ── Brief lock g2: two currencies ─────────────────────────────────────────────────


async def test_two_currencies_are_summed_apart(
    session_factory: Factory, worlds: list[World]
) -> None:
    world = await _world(session_factory, worlds, {"alice": False})
    alice = world.learners["alice"]
    await _buy(session_factory, alice.account_id, 100, 1000, "USD", DAY_2)
    await _buy(session_factory, alice.account_id, 100, 1200, "EUR", DAY_1)
    await _review(session_factory, world, "alice", world.task_a, 130)

    report = await _report(session_factory, world)

    row = _task_row(report, world.task_a)
    assert row.reviews.money_minor == (("EUR", 1200), ("USD", 300)), (
        "100 units of the euro batch (taken first), 30 of the dollar one"
    )
    table = billing_admin._render_cost_table(report, tenant_label=None)
    assert "EUR 1200; USD 300" in table
    rows = list(csv.DictReader(io.StringIO(billing_admin._render_cost_csv(report))))
    total = next(r for r in rows if r["row_kind"] == "total")
    assert total["students_money_minor"] == "EUR 1200; USD 300"
