"""BIL-COSTS Б-1: what students paid for reviews, by course and student (live DB).

``GET /billing/students-money`` is the author's view of the money the cost
report (``costs``) already counts for the administrator. The journal is written
by the ledger itself (``credit_purchase`` / ``credit_admin`` / ``settle``) at
fixed instants in October 2026, so the period is exact whatever day the test
runs on; batches are taken in the order they expire, so a batch bought earlier
pays first. The locks are named by the letters of the task (а)-(ж).

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.billing import (
    costs,
    ledger,
    review_money,
    students_money,
    testmark,
)
from course_supporter.billing.accounts import (
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.kinds import ActionKind, MarkOrigin, PayerSource
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingAccount,
    BillingBatch,
    BillingTestBatch,
    CourseNode,
    HomeworkSubmission,
    Student,
    Tenant,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

OCTOBER = {"from": "2026-10-01", "to": "2026-10-31"}
OCTOBER_FROM = datetime(2026, 10, 1, tzinfo=UTC).date()
OCTOBER_TO = datetime(2026, 10, 31, tzinfo=UTC).date()
IN_OCTOBER = datetime(2026, 10, 15, 12, tzinfo=UTC)
DAY_1 = datetime(2026, 9, 1, tzinfo=UTC)
DAY_2 = datetime(2026, 9, 2, tzinfo=UTC)
DAY_3 = datetime(2026, 9, 3, tzinfo=UTC)
ROUTE = "/api/v1/billing/students-money"

Factory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Learner:
    student_id: uuid.UUID
    account_id: uuid.UUID


@dataclass(frozen=True)
class Course:
    node_id: uuid.UUID
    task_id: uuid.UUID


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    courses: dict[str, Course]
    author_account: uuid.UUID
    learners: dict[str, Learner]


@dataclass(frozen=True)
class Who:
    """A student of the world: its name; a service student; a deleted one."""

    name: str | None = None
    service: bool = False
    deleted: bool = False


async def _make_world(
    factory: Factory, courses: list[str], learners: dict[str, Who]
) -> World:
    """One tenant, courses (a task each), a student and an account for each name."""
    async with factory() as session:
        tenant = Tenant(name=f"bil-costs-b1-{uuid.uuid4().hex[:8]}")
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
        students = {
            key: Student(
                tenant_id=tenant.id,
                external_id=f"{key}-{uuid.uuid4().hex[:8]}",
                display_name=who.name,
                is_test_student=who.service,
                deleted_at=datetime(2026, 10, 20, tzinfo=UTC) if who.deleted else None,
            )
            for key, who in learners.items()
        }
        session.add_all(students.values())
        await session.flush()
        author = await get_or_create_author_account(session, tenant_id=tenant.id)
        accounts = {
            key: await get_or_create_student_account(
                session, tenant_id=tenant.id, student_id=student.id
            )
            for key, student in students.items()
        }
        await session.commit()
        return World(
            tenant_id=tenant.id,
            courses={
                title: Course(nodes[title].id, tasks[title].id) for title in nodes
            },
            author_account=author.id,
            learners={
                key: Learner(students[key].id, accounts[key].id) for key in students
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
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == world.tenant_id
            )
        )
        await session.commit()
    await drop_billing_of_tenant(factory, world.tenant_id)
    async with factory() as session:
        roots = [c.node_id for c in world.courses.values()]
        await session.execute(
            delete(AuthoredDocument).where(AuthoredDocument.course_root_id.in_(roots))
        )
        await session.execute(delete(CourseNode).where(CourseNode.id.in_(roots)))
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
    factory: Factory,
    worlds: list[World],
    courses: list[str],
    learners: dict[str, Who],
) -> World:
    world = await _make_world(factory, courses, learners)
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
    course: str,
    units: int,
    *,
    at: datetime = IN_OCTOBER,
    payer: uuid.UUID | None = None,
) -> uuid.UUID:
    """A review of the student's work on the course's task, settled to ``units``;
    the student's own account pays, or ``payer`` (the author under a rule)."""
    learner = world.learners[who]
    node = world.courses[course]
    async with factory() as session:
        submission = HomeworkSubmission(
            tenant_id=world.tenant_id,
            student_id=learner.student_id,
            course_node_id=node.node_id,
            node_id=node.node_id,
            authored_document_id=node.task_id,
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


async def _review_of_nothing(
    factory: Factory, world: World, who: str, units: int
) -> None:
    """A review whose submission is gone: the journal's key leads nowhere."""
    learner = world.learners[who]
    async with factory() as session:
        await ledger.settle(
            session,
            action_kind=ActionKind.REVIEW,
            action_key=str(uuid.uuid4()),
            fact_units=units,
            beneficiary_account_id=learner.account_id,
            payer_source=PayerSource.DEFAULT,
            account_id=learner.account_id,
            now=IN_OCTOBER,
        )
        await session.commit()


# ── The route, as the tenant of a world ─────────────────────────────────────────


class _Caller:
    """Who the API key is: switchable inside a test, to be two tenants in turn."""

    tenant_id: uuid.UUID
    scopes: list[str]

    def __init__(self) -> None:
        self.scopes = ["prep"]


@pytest.fixture()
def caller(session_factory: Factory) -> Iterator[_Caller]:
    current = _Caller()

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
        tenant_id=current.tenant_id,
        tenant_name="t",
        scopes=current.scopes,
        plan_id="basic",
        key_prefix="cs_test",
    )
    yield current
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _get(
    caller: _Caller, world: World, params: dict[str, str] | None = None
) -> Any:
    caller.tenant_id = world.tenant_id
    async with _client() as client:
        return await client.get(ROUTE, params=OCTOBER if params is None else params)


def _body(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _eur(minor: int) -> list[dict[str, Any]]:
    return [{"currency": "EUR", "amount_minor": minor}]


NOTHING = {"reviews": 0, "units": 0, "money": [], "gift_units": 0}


async def _seed_the_main_world(factory: Factory, worlds: list[World]) -> World:
    """Three students, three courses and a fourth without reviews.

    - alice: two batches of different price (5 and 15 a unit); one review of 100
      units in «Омега» (60 from the first batch, 40 from the second).
    - bob: a batch of 5 a unit; 20 units in «Омега», 30 in «Мала».
    - carol: a batch of 10 a unit; 20 units in «Омега», 30 in «Гамма».
    """
    world = await _world(
        factory,
        worlds,
        ["Мала", "Омега", "Гамма", "Порожній"],
        {
            "alice": Who("Аліса"),
            "bob": Who("Богдан"),
            "carol": Who("Кароліна"),
        },
    )
    alice, bob, carol = (world.learners[n] for n in ("alice", "bob", "carol"))
    await _buy(factory, alice.account_id, 60, 300, "EUR", DAY_1)
    await _buy(factory, alice.account_id, 100, 1500, "EUR", DAY_2)
    await _buy(factory, bob.account_id, 200, 1000, "EUR", DAY_1)
    await _buy(factory, carol.account_id, 100, 1000, "EUR", DAY_1)
    await _review(factory, world, "alice", "Омега", 100)
    await _review(factory, world, "bob", "Омега", 20)
    await _review(factory, world, "bob", "Мала", 30)
    await _review(factory, world, "carol", "Омега", 20)
    await _review(factory, world, "carol", "Гамма", 30)
    return world


# ── (а) the money of each is the sum of the shares; the order is by units ──────────


@pytest.mark.usefixtures("caller")
async def test_a_the_money_is_the_sum_of_shares_and_the_order_is_by_units(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _seed_the_main_world(session_factory, worlds)
    alice, bob, carol = (world.learners[n] for n in ("alice", "bob", "carol"))
    omega, small, gamma = (world.courses[t] for t in ("Омега", "Мала", "Гамма"))

    body = _body(await _get(caller, world))

    assert body["from"] == "2026-10-01"
    assert body["to"] == "2026-10-31"
    assert body["totals"] == {
        "reviews": 5,
        "units": 200,
        "money": _eur(60 * 5 + 40 * 15 + 20 * 5 + 30 * 5 + 20 * 10 + 30 * 10),
        "gift_units": 0,
        "excluded_test_reviews": 0,
    }
    assert body["unlinked"] == NOTHING
    assert [c["title"] for c in body["courses"][:1]] == ["Омега"], (
        "the biggest by units first, whatever the title"
    )
    tied_by_id = sorted([small.node_id, gamma.node_id], key=str)
    assert [c["course_node_id"] for c in body["courses"]] == [
        str(omega.node_id),
        *map(str, tied_by_id),
    ], "30 and 30 units: by course_node_id; «Порожній» has no reviews: not shown"

    first = body["courses"][0]
    tied_students = sorted([bob.student_id, carol.student_id], key=str)
    names = {bob.student_id: "Богдан", carol.student_id: "Кароліна"}
    assert first["students"] == [
        {
            "student_id": str(alice.student_id),
            "display_name": "Аліса",
            "deleted": False,
            "reviews": 1,
            "units": 100,
            "money": _eur(60 * 5 + 40 * 15),
            "gift_units": 0,
        },
        *(
            {
                "student_id": str(student),
                "display_name": names[student],
                "deleted": False,
                "reviews": 1,
                "units": 20,
                "money": _eur(20 * (5 if student == bob.student_id else 10)),
                "gift_units": 0,
            }
            for student in tied_students
        ),
    ], "alice 100 units; bob and carol 20 each: by student_id"
    assert {k: v for k, v in first.items() if k != "students"} == {
        "course_node_id": str(omega.node_id),
        "title": "Омега",
        "reviews": 3,
        "units": 140,
        "money": _eur(900 + 100 + 200),
        "gift_units": 0,
    }


# ── (б) a gift and a marked test batch are gift units, not money ───────────────────


@pytest.mark.usefixtures("caller")
async def test_b_gift_and_marked_test_batches_are_gift_units_not_money(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _world(session_factory, worlds, ["Курс"], {"hank": Who("Генк")})
    hank = world.learners["hank"]
    await _gift(session_factory, hank.account_id, 50, DAY_1)
    await _buy(
        session_factory, hank.account_id, 40, 400, "EUR", DAY_2, test_purchase=True
    )
    await _buy(session_factory, hank.account_id, 100, 1000, "EUR", DAY_3)
    await _review(session_factory, world, "hank", "Курс", 90)  # the gift + the test
    await _review(session_factory, world, "hank", "Курс", 10)  # the paid one

    body = _body(await _get(caller, world))

    expected = {"reviews": 2, "units": 100, "money": _eur(100), "gift_units": 90}
    assert {
        k: v for k, v in body["totals"].items() if k != "excluded_test_reviews"
    } == expected
    course = body["courses"][0]
    assert {k: v for k, v in course.items() if k in expected} == expected
    assert {
        k: v for k, v in course["students"][0].items() if k in expected
    } == expected, (
        "50 of an admin credit + 40 of a marked purchase are gift units; only the "
        "10 units of the paid batch are money"
    )


# ── (в) a service student is not in the rows, and is counted ─────────────────────


@pytest.mark.usefixtures("caller")
async def test_v_a_service_student_is_not_in_the_rows_only_counted(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _world(
        session_factory,
        worlds,
        ["Курс"],
        {"alice": Who("Аліса"), "svc": Who("Службовий", service=True)},
    )
    for who in ("alice", "svc"):
        await _buy(
            session_factory, world.learners[who].account_id, 100, 1000, "EUR", DAY_1
        )
    await _review(session_factory, world, "alice", "Курс", 10)
    await _review(session_factory, world, "svc", "Курс", 20)
    await _review(session_factory, world, "svc", "Курс", 5)

    body = _body(await _get(caller, world))

    assert body["totals"] == {
        "reviews": 1,
        "units": 10,
        "money": _eur(100),
        "gift_units": 0,
        "excluded_test_reviews": 2,
    }
    assert [s["student_id"] for s in body["courses"][0]["students"]] == [
        str(world.learners["alice"].student_id)
    ]


# ── (г) a deleted student keeps the row, without a name ──────────────────────────


@pytest.mark.usefixtures("caller")
async def test_g_a_deleted_student_keeps_the_row_without_a_name(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _world(
        session_factory,
        worlds,
        ["Курс"],
        {"alice": Who("Аліса"), "gone": Who("Видалений", deleted=True)},
    )
    for who in ("alice", "gone"):
        await _buy(
            session_factory, world.learners[who].account_id, 100, 1000, "EUR", DAY_1
        )
    await _review(session_factory, world, "alice", "Курс", 10)
    await _review(session_factory, world, "gone", "Курс", 40)

    body = _body(await _get(caller, world))

    gone, alice = body["courses"][0]["students"]
    assert gone == {
        "student_id": str(world.learners["gone"].student_id),
        "display_name": None,
        "deleted": True,
        "reviews": 1,
        "units": 40,
        "money": _eur(400),
        "gift_units": 0,
    }
    assert (alice["display_name"], alice["deleted"]) == ("Аліса", False)
    assert body["totals"]["money"] == _eur(500), "the deleted student's money is kept"


# ── (ґ) another tenant is not seen, and does not see ours ────────────────────────


@pytest.mark.usefixtures("caller")
async def test_gg_another_tenants_courses_and_students_are_not_seen(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    ours = await _seed_the_main_world(session_factory, worlds)
    theirs = await _world(session_factory, worlds, ["Чужий курс"], {"eve": Who("Єва")})
    await _buy(
        session_factory, theirs.learners["eve"].account_id, 100, 1000, "USD", DAY_1
    )
    await _review(session_factory, theirs, "eve", "Чужий курс", 77)

    our_body = _body(await _get(caller, ours))
    their_body = _body(await _get(caller, theirs))

    assert our_body["totals"]["units"] == 200
    assert our_body["totals"]["money"] == _eur(1650)
    assert "Чужий курс" not in [c["title"] for c in our_body["courses"]]
    everyone = {s["student_id"] for c in our_body["courses"] for s in c["students"]}
    assert str(theirs.learners["eve"].student_id) not in everyone
    assert their_body["totals"] == {
        "reviews": 1,
        "units": 77,
        "money": [{"currency": "USD", "amount_minor": 770}],
        "gift_units": 0,
        "excluded_test_reviews": 0,
    }, "and with their key, only theirs"
    assert [c["title"] for c in their_body["courses"]] == ["Чужий курс"]
    assert their_body["unlinked"] == NOTHING


async def test_gg_own_courses_excludes_other_tenants_courses(
    session_factory: Factory, worlds: list[World]
) -> None:
    ours = await _world(session_factory, worlds, ["Наш курс"], {})
    theirs = await _world(session_factory, worlds, ["Чужий курс"], {})
    our_course = ours.courses["Наш курс"].node_id
    their_course = theirs.courses["Чужий курс"].node_id

    async with session_factory() as session:
        titles = await students_money._own_courses(
            session, ours.tenant_id, {our_course, their_course}
        )

    assert their_course not in titles
    assert titles == {our_course: "Наш курс"}


async def test_gg_own_students_excludes_other_tenants_students(
    session_factory: Factory, worlds: list[World]
) -> None:
    ours = await _world(
        session_factory,
        worlds,
        ["Наш курс"],
        {"alice": Who("Аліса"), "gone": Who("Видалений", deleted=True)},
    )
    theirs = await _world(session_factory, worlds, ["Чужий курс"], {"eve": Who("Єва")})
    alice_id = ours.learners["alice"].student_id
    gone_id = ours.learners["gone"].student_id
    eve_id = theirs.learners["eve"].student_id

    async with session_factory() as session:
        students = await students_money._own_students(
            session, ours.tenant_id, {alice_id, gone_id, eve_id}
        )

    assert eve_id not in students
    assert students == {
        alice_id: ("Аліса", False),
        gone_id: (None, True),
    }


# ── (д) the period: both days included, invalid_period, an empty period ──────────


@pytest.mark.usefixtures("caller")
async def test_d_the_period_includes_both_days(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _world(session_factory, worlds, ["Курс"], {"alice": Who("Аліса")})
    await _buy(
        session_factory, world.learners["alice"].account_id, 1000, 10000, "EUR", DAY_1
    )
    for at, units in (
        (datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC), 1),  # the day before
        (datetime(2026, 10, 1, tzinfo=UTC), 2),  # the first second
        (datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC), 4),  # the last second
        (datetime(2026, 11, 1, tzinfo=UTC), 8),  # the day after
    ):
        await _review(session_factory, world, "alice", "Курс", units, at=at)

    body = _body(await _get(caller, world))

    assert body["totals"]["units"] == 6, "2 + 4: the first and the last second"
    assert body["totals"]["reviews"] == 2
    one_day = _body(
        await _get(caller, world, {"from": "2026-10-31", "to": "2026-10-31"})
    )
    assert one_day["totals"]["units"] == 4, "from == to is one whole day"


@pytest.mark.usefixtures("caller")
async def test_d_an_upside_down_period_is_invalid_period(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _world(session_factory, worlds, ["Курс"], {"alice": Who("Аліса")})

    response = await _get(caller, world, {"from": "2026-10-31", "to": "2026-10-01"})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_period"


@pytest.mark.usefixtures("caller")
async def test_d_an_empty_period_is_empty_lists_and_zeros(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _seed_the_main_world(session_factory, worlds)

    body = _body(await _get(caller, world, {"from": "2026-01-01", "to": "2026-01-31"}))

    assert body["courses"] == []
    assert body["totals"] == {**NOTHING, "excluded_test_reviews": 0}
    assert body["unlinked"] == NOTHING


@pytest.mark.usefixtures("caller")
async def test_d_without_a_period_it_is_the_current_month(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _seed_the_main_world(session_factory, worlds)  # October 2026

    body = _body(await _get(caller, world, {}))

    first, today = costs.default_month_bounds()
    assert (body["from"], body["to"]) == (first.isoformat(), today.isoformat())


# ── (е) two currencies are two sums ───────────────────────────────────────────────


@pytest.mark.usefixtures("caller")
async def test_e_two_currencies_are_summed_apart(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _world(
        session_factory,
        worlds,
        ["Курс"],
        {"alice": Who("Аліса"), "bob": Who("Богдан")},
    )
    alice, bob = world.learners["alice"], world.learners["bob"]
    await _buy(session_factory, alice.account_id, 100, 1000, "USD", DAY_1)
    await _buy(session_factory, alice.account_id, 100, 500, "EUR", DAY_2)
    await _buy(session_factory, bob.account_id, 100, 700, "EUR", DAY_1)
    await _review(session_factory, world, "alice", "Курс", 150)  # 100 USD, 50 EUR
    await _review(session_factory, world, "bob", "Курс", 10)

    body = _body(await _get(caller, world))

    alice_row, bob_row = body["courses"][0]["students"]
    assert alice_row["money"] == [
        {"currency": "EUR", "amount_minor": 250},
        {"currency": "USD", "amount_minor": 1000},
    ], "sorted by currency; units are 150 whatever the currency"
    assert alice_row["units"] == 150
    assert bob_row["money"] == _eur(70)
    assert body["totals"]["money"] == [
        {"currency": "EUR", "amount_minor": 320},
        {"currency": "USD", "amount_minor": 1000},
    ]


# ── (є) the key of a student or the portal is no key of an author ───────────────


async def test_ye_a_key_without_the_prep_scope_is_refused(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _world(session_factory, worlds, ["Курс"], {"alice": Who("Аліса")})
    caller.scopes = ["check"]

    response = await _get(caller, world)

    assert response.status_code == 403
    assert (await _get_spending(caller, world)).status_code == 403, "as spending"


async def test_ye_no_key_and_a_foreign_key_are_401_as_in_the_neighbours(
    session_factory: Factory,
) -> None:
    async with _client() as client:
        for headers in ({}, {"X-API-Key": "a-portal-token-is-not-a-key"}):
            ours = await client.get(ROUTE, params=OCTOBER, headers=headers)
            neighbour = await client.get(
                "/api/v1/billing/spending", params=OCTOBER, headers=headers
            )
            assert ours.status_code == neighbour.status_code
            assert ours.status_code in (401, 403)


async def _get_spending(caller: _Caller, world: World) -> Any:
    caller.tenant_id = world.tenant_id
    async with _client() as client:
        return await client.get("/api/v1/billing/spending", params=OCTOBER)


# ── What is not the students' money ─────────────────────────────────────────────


@pytest.mark.usefixtures("caller")
async def test_a_review_the_author_paid_is_not_shown(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _world(session_factory, worlds, ["Курс"], {"alice": Who("Аліса")})
    alice = world.learners["alice"]
    await _buy(session_factory, world.author_account, 100, 1000, "EUR", DAY_1)
    await _buy(session_factory, alice.account_id, 100, 1000, "EUR", DAY_1)
    await _review(
        session_factory, world, "alice", "Курс", 40, payer=world.author_account
    )
    await _review(session_factory, world, "alice", "Курс", 20)

    body = _body(await _get(caller, world))

    assert body["totals"]["units"] == 20
    assert body["totals"]["money"] == _eur(200)
    assert body["courses"][0]["students"][0]["reviews"] == 1


@pytest.mark.usefixtures("caller")
async def test_a_review_that_leads_to_no_course_is_in_the_totals_and_unlinked(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _world(session_factory, worlds, ["Курс"], {"alice": Who("Аліса")})
    await _buy(
        session_factory, world.learners["alice"].account_id, 100, 1000, "EUR", DAY_1
    )
    await _review(session_factory, world, "alice", "Курс", 10)
    await _review_of_nothing(session_factory, world, "alice", 5)

    body = _body(await _get(caller, world))

    assert body["totals"]["units"] == 15
    assert body["totals"]["money"] == _eur(150)
    assert body["unlinked"] == {
        "reviews": 1,
        "units": 5,
        "money": _eur(50),
        "gift_units": 0,
    }
    assert body["courses"][0]["units"] == 10, "nothing is lost, nothing is in a course"


@pytest.mark.usefixtures("caller")
async def test_the_money_is_rounded_once_per_row_half_up(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _world(
        session_factory,
        worlds,
        ["Курс"],
        {"alice": Who("Аліса"), "bob": Who("Богдан")},
    )
    alice, bob = world.learners["alice"], world.learners["bob"]
    await _buy(session_factory, alice.account_id, 7, 100, "EUR", DAY_1)  # 14.29 a unit
    await _review(session_factory, world, "alice", "Курс", 1)
    await _review(session_factory, world, "alice", "Курс", 1)
    await _buy(session_factory, bob.account_id, 2, 1, "EUR", DAY_1)  # 0.5 a unit
    await _review(session_factory, world, "bob", "Курс", 1)

    body = _body(await _get(caller, world))

    by_name = {s["display_name"]: s for s in body["courses"][0]["students"]}
    assert by_name["Аліса"]["money"] == _eur(29), "28.57, not 14 + 14"
    assert by_name["Богдан"]["money"] == _eur(1), "half goes up"
    assert body["courses"][0]["money"] == _eur(29), "29.07, not the 29 + 1 of the rows"
    assert body["totals"]["money"] == _eur(29)


# ── (ж) one function counts the money: costs and the route agree ────────────────


async def test_zh_costs_and_the_route_give_the_same_numbers_on_one_seed(
    session_factory: Factory, worlds: list[World], caller: _Caller
) -> None:
    world = await _seed_the_main_world(session_factory, worlds)
    await _buy(
        session_factory, world.learners["alice"].account_id, 40, 400, "USD", DAY_3
    )
    # Alice's older EUR batch pays first: 60 units at 15 a unit, then 10 of USD.
    await _review(session_factory, world, "alice", "Гамма", 70)
    body = _body(await _get(caller, world))

    async with session_factory() as session:
        by_task = await costs.build_cost_report(
            session,
            from_date=OCTOBER_FROM,
            to_date=OCTOBER_TO,
            tenant_id=world.tenant_id,
            by="task",
        )
        by_author = await costs.build_cost_report(
            session,
            from_date=OCTOBER_FROM,
            to_date=OCTOBER_TO,
            tenant_id=world.tenant_id,
            by="author",
        )

    def figures(reviews: costs.ReviewFigures, units: int) -> dict[str, Any]:
        return {
            "reviews": reviews.count,
            "units": units,
            "money": [
                {"currency": c, "amount_minor": m} for c, m in reviews.money_minor
            ],
            "gift_units": reviews.gift_units,
        }

    totals = by_author.authors[0]
    assert {
        k: v for k, v in body["totals"].items() if k != "excluded_test_reviews"
    } == figures(totals.reviews, totals.units.students_units)
    assert body["totals"]["money"] == [
        {"currency": "EUR", "amount_minor": 1650 + 60 * 15},
        {"currency": "USD", "amount_minor": 10 * 10},
    ]
    for task_row in by_task.tasks:
        course = next(
            c
            for c in body["courses"]
            if c["course_node_id"] == str(task_row.course_node_id)
        )
        assert {k: v for k, v in course.items() if k in NOTHING} == figures(
            task_row.reviews, task_row.units.students_units
        ), course["title"]
    assert body["totals"]["money"] == figures(by_task.tasks_total.reviews, 0)["money"]


def test_zh_the_money_is_counted_by_one_function() -> None:
    assert vars(costs)["review_debits"] is review_money.review_debits
    assert vars(students_money)["review_debits"] is review_money.review_debits
    assert costs.ReviewFigures is review_money.ReviewFigures
