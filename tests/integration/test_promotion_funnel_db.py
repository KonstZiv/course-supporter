"""Б-4: the promotion path of a tenant and the funnel, on a live database.

Locks of the task:

* (а) ``set-promotion-path``: set, change, clear (``none``); an invalid value
  is refused (the command line and the database CHECK); the same value again
  writes nothing (no log line).
* (ґ) the funnel on a seed: 3 enrolled → 2 submitted → 1 bought → 1 bought
  again; a gift and a test purchase are not purchases, nor are a purchase and
  a submission before the event; a service student is left out, and counted
  with ``--include-test-students``.
* (д) the scopes ``path``, ``tenant``, ``course``, ``source`` group right;
  ``purchased_money`` only by ``path`` / ``tenant``, not doubled for a student
  in two courses; two currencies apart. The command's text, csv and json.

The seed is dated in 2025 (events, submissions, purchases at fixed instants),
a period no other test writes into.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest
from scripts import billing_admin
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.billing import ledger, testmark
from course_supporter.billing.accounts import get_or_create_student_account
from course_supporter.billing.errors import BillingNotFoundError
from course_supporter.billing.funnel import FunnelReport, FunnelRow, build_funnel
from course_supporter.billing.kinds import MarkOrigin
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingAccount,
    BillingBatch,
    BillingTestBatch,
    CourseNode,
    EnrollmentEvent,
    HomeworkSubmission,
    Student,
    Tenant,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

Factory = async_sessionmaker[AsyncSession]
FROM, TO = date(2025, 3, 1), date(2025, 3, 31)


def at(day: int, month: int = 3) -> datetime:
    return datetime(2025, month, day, 12, tzinfo=UTC)


@dataclass
class Seed:
    """What a test made; dropped after it."""

    tenants: list[uuid.UUID] = field(default_factory=list)


@pytest.fixture()
async def seed(session_factory: Factory) -> AsyncGenerator[Seed]:
    made = Seed()
    yield made
    for tenant_id in made.tenants:
        async with session_factory() as session:
            accounts = select(BillingAccount.id).where(
                BillingAccount.tenant_id == tenant_id
            )
            batches = select(BillingBatch.id).where(
                BillingBatch.account_id.in_(accounts)
            )
            await session.execute(
                delete(BillingTestBatch).where(BillingTestBatch.batch_id.in_(batches))
            )
            await session.execute(
                delete(HomeworkSubmission).where(
                    HomeworkSubmission.tenant_id == tenant_id
                )
            )
            await session.commit()
        await drop_billing_of_tenant(session_factory, tenant_id)
        async with session_factory() as session:
            courses = select(CourseNode.id).where(CourseNode.tenant_id == tenant_id)
            await session.execute(
                delete(AuthoredDocument).where(
                    AuthoredDocument.course_root_id.in_(courses)
                )
            )
            # Events, students and courses go with the tenant (CASCADE).
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.commit()


# ── Building a world ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Place:
    tenant_id: uuid.UUID
    tenant_name: str
    courses: dict[str, tuple[uuid.UUID, uuid.UUID]]
    """title → (course id, its task id)."""


async def _tenant(
    factory: Factory, seed: Seed, path: str | None, courses: list[str]
) -> Place:
    async with factory() as session:
        tenant = Tenant(name=f"b4-{uuid.uuid4().hex[:8]}", promotion_path=path)
        session.add(tenant)
        await session.flush()
        made: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
        for i, title in enumerate(courses):
            course = make_root_course_node(tenant_id=tenant.id, title=title, order=i)
            session.add(course)
            await session.flush()
            task = AuthoredDocument(
                course_node_id=course.id,
                course_root_id=course.id,
                source_type="text",
                source_url=f"https://example.com/{uuid.uuid4().hex}",
                title="task",
                task_type="task",
            )
            session.add(task)
            await session.flush()
            made[title] = (course.id, task.id)
        await session.commit()
        seed.tenants.append(tenant.id)
        return Place(tenant.id, tenant.name, made)


async def _student(
    factory: Factory, place: Place, *, service: bool = False
) -> uuid.UUID:
    async with factory() as session:
        student = Student(
            tenant_id=place.tenant_id,
            external_id=f"b4-{uuid.uuid4().hex[:8]}",
            is_test_student=service,
        )
        session.add(student)
        await session.commit()
        return student.id


async def _enrolled(
    factory: Factory,
    place: Place,
    course: str,
    student_id: uuid.UUID,
    when: datetime,
    *,
    source: str = "self",
    tag: str | None = None,
) -> None:
    async with factory() as session:
        session.add(
            EnrollmentEvent(
                tenant_id=place.tenant_id,
                course_node_id=place.courses[course][0],
                student_id=student_id,
                source=source,
                source_tag=tag,
                created_at=when,
            )
        )
        await session.commit()


async def _submitted(
    factory: Factory, place: Place, course: str, student_id: uuid.UUID, when: datetime
) -> None:
    course_id, task_id = place.courses[course]
    async with factory() as session:
        session.add(
            HomeworkSubmission(
                tenant_id=place.tenant_id,
                student_id=student_id,
                course_node_id=course_id,
                node_id=course_id,
                authored_document_id=task_id,
                file_url="s3://bucket/work.py",
                file_type="text/plain",
                original_filename="work.py",
                status="completed",
                created_at=when,
            )
        )
        await session.commit()


async def _account(factory: Factory, place: Place, student_id: uuid.UUID) -> uuid.UUID:
    async with factory() as session:
        account = await get_or_create_student_account(
            session, tenant_id=place.tenant_id, student_id=student_id
        )
        await session.commit()
        return account.id


async def _bought(
    factory: Factory,
    place: Place,
    student_id: uuid.UUID,
    minor: int,
    when: datetime,
    *,
    currency: str = "EUR",
    test: bool = False,
) -> None:
    account_id = await _account(factory, place, student_id)
    async with factory() as session:
        credit = await ledger.credit_purchase(
            session,
            account_id=account_id,
            units=100,
            amount_paid_minor=minor,
            currency=currency,
            provider="fake",
            now=when,
        )
        if test:
            await testmark.mark(
                session, batch_id=credit.batch.id, origin=MarkOrigin.CREDIT, now=when
            )
        await session.commit()


async def _gifted(
    factory: Factory, place: Place, student_id: uuid.UUID, when: datetime
) -> None:
    account_id = await _account(factory, place, student_id)
    async with factory() as session:
        await ledger.credit_admin(
            session,
            account_id=account_id,
            units=500,
            reason="pilot",
            actor="test",
            now=when,
        )
        await session.commit()


async def _funnel(
    factory: Factory, by: str, *, include_test_students: bool = False
) -> FunnelReport:
    async with factory() as session:
        return await build_funnel(
            session,
            from_date=FROM,
            to_date=TO,
            by=by,  # type: ignore[arg-type]
            include_test_students=include_test_students,
        )


def _figures(row: FunnelRow) -> tuple[int, int, int, int]:
    return (row.enrolled, row.first_submission, row.first_purchase, row.repeat_purchase)


async def _own_course_world(factory: Factory, seed: Seed) -> Place:
    """The seed of lock (ґ): one tenant, own_course, one course."""
    place = await _tenant(factory, seed, "own_course", ["Python"])
    buyer, gifted, late, service = (
        await _student(factory, place),
        await _student(factory, place),
        await _student(factory, place),
        await _student(factory, place, service=True),
    )
    for student in (buyer, gifted, late):
        await _enrolled(factory, place, "Python", student, at(5), tag="fb-dec-ads")
    await _enrolled(factory, place, "Python", service, at(6), tag="fb-dec-ads")
    # Outside the period: not counted, though the same student.
    await _enrolled(factory, place, "Python", buyer, at(2, month=4))

    await _submitted(factory, place, "Python", buyer, at(7))
    await _submitted(factory, place, "Python", gifted, at(8))
    await _submitted(factory, place, "Python", late, at(1))  # before the event
    await _submitted(factory, place, "Python", service, at(9))

    await _bought(factory, place, buyer, 1000, at(10))
    await _bought(factory, place, buyer, 500, at(20))
    await _gifted(factory, place, gifted, at(10))  # a gift: not a purchase
    await _bought(factory, place, gifted, 900, at(11), test=True)  # a test one
    await _bought(factory, place, late, 700, at(2))  # before the event
    await _bought(factory, place, service, 300, at(12))
    return place


# ── (а) The promotion path ─────────────────────────────────────────────────


def _path_cmd(tenant: str, path: str) -> list[str]:
    return ["set-promotion-path", "--tenant", tenant, "--path", path, "--actor", "kost"]


async def _path_of(factory: Factory, tenant_id: uuid.UUID) -> str | None:
    async with factory() as session:
        return await session.scalar(
            select(Tenant.promotion_path).where(Tenant.id == tenant_id)
        )


async def _run(argv: list[str]) -> str:
    return await billing_admin.run(billing_admin.build_parser().parse_args(argv))


class TestPromotionPath:
    async def test_set_change_clear_and_the_same_value_writes_nothing(
        self, session_factory: Factory, seed: Seed
    ) -> None:
        place = await _tenant(session_factory, seed, None, [])
        with capture_logs() as logs:
            first = await _run(_path_cmd(place.tenant_name, "school"))
            again = await _run(_path_cmd(str(place.tenant_id), "school"))
            changed = await _run(_path_cmd(place.tenant_name, "author"))
            cleared = await _run(_path_cmd(place.tenant_name, "none"))
            cleared_again = await _run(_path_cmd(place.tenant_name, "none"))

        assert (
            first == f"changed: tenant {place.tenant_id}  promotion_path none → school"
        )
        assert again.startswith("unchanged (already so)")
        assert changed.endswith("promotion_path school → author")
        assert cleared.endswith("promotion_path author → none")
        assert cleared_again.startswith("unchanged (already so)")
        assert await _path_of(session_factory, place.tenant_id) is None
        lines = [e for e in logs if e["event"] == "tenant_promotion_path_set"]
        assert [(e["before"], e["after"]) for e in lines] == [
            (None, "school"),
            ("school", "author"),
            ("author", None),
        ], "a repeat writes no line"
        assert {e["actor"] for e in lines} == {"kost"}
        assert {e["tenant_id"] for e in lines} == {str(place.tenant_id)}

    async def test_an_invalid_value_is_refused(
        self, session_factory: Factory, seed: Seed
    ) -> None:
        place = await _tenant(session_factory, seed, "school", [])
        with pytest.raises(SystemExit) as exit_info:
            billing_admin.build_parser().parse_args(
                _path_cmd(place.tenant_name, "partner")
            )
        assert exit_info.value.code == 2
        async with session_factory() as session:
            with pytest.raises(IntegrityError, match="ck_tenants_promotion_path"):
                await session.execute(
                    text("UPDATE tenants SET promotion_path = 'partner' WHERE id = :t"),
                    {"t": place.tenant_id},
                )
        assert await _path_of(session_factory, place.tenant_id) == "school"

    async def test_an_unknown_tenant_is_refused(self) -> None:
        with pytest.raises(BillingNotFoundError):
            await _run(_path_cmd("no-such-tenant-b4", "school"))


# ── (ґ) The funnel on a seed ───────────────────────────────────────────────


class TestFunnel:
    async def test_three_two_one_one_without_the_service_student(
        self, session_factory: Factory, seed: Seed
    ) -> None:
        place = await _own_course_world(session_factory, seed)

        report = await _funnel(session_factory, "tenant")

        row = next(r for r in report.rows if r.group.tenant_id == place.tenant_id)
        assert _figures(row) == (3, 2, 1, 1)
        assert row.purchased_money == (("EUR", 1500),), "gift, test, earlier: out"
        assert report.excluded_test_students == 1

    async def test_with_the_service_student(
        self, session_factory: Factory, seed: Seed
    ) -> None:
        place = await _own_course_world(session_factory, seed)

        report = await _funnel(session_factory, "tenant", include_test_students=True)

        row = next(r for r in report.rows if r.group.tenant_id == place.tenant_id)
        assert _figures(row) == (4, 3, 2, 1)
        assert row.purchased_money == (("EUR", 1800),)
        assert report.excluded_test_students == 0


# ── (д) The scopes and the money ───────────────────────────────────────────


async def _two_tenants(factory: Factory, seed: Seed) -> tuple[Place, Place]:
    """(ґ)'s tenant, plus a school with two courses: ``both`` is enrolled in
    each (two purchases in two currencies after them), ``granted`` by the
    author."""
    own = await _own_course_world(factory, seed)
    school = await _tenant(factory, seed, "school", ["Data", "Web"])
    both, granted = await _student(factory, school), await _student(factory, school)
    await _enrolled(factory, school, "Data", both, at(3), tag="ig-x")
    await _enrolled(factory, school, "Web", both, at(4), tag="ig-x")
    await _enrolled(factory, school, "Data", granted, at(4), source="author")
    await _bought(factory, school, both, 2000, at(15), currency="USD")
    await _bought(factory, school, both, 100, at(16), currency="EUR")
    await _submitted(factory, school, "Web", both, at(17))
    return own, school


class TestScopes:
    async def test_by_path(self, session_factory: Factory, seed: Seed) -> None:
        await _two_tenants(session_factory, seed)

        report = await _funnel(session_factory, "path")

        rows = {r.group.path: r for r in report.rows}
        assert list(rows) == ["own_course", "school"], "declared order"
        assert _figures(rows["own_course"]) == (3, 2, 1, 1)
        assert rows["own_course"].purchased_money == (("EUR", 1500),)
        assert _figures(rows["school"]) == (3, 1, 2, 2)
        assert rows["school"].purchased_money == (("EUR", 100), ("USD", 2000)), (
            "one student in two courses: the tenant's money once"
        )
        assert _figures(report.total) == (6, 3, 3, 3)
        assert report.total.purchased_money == (("EUR", 1600), ("USD", 2000))

    async def test_by_tenant(self, session_factory: Factory, seed: Seed) -> None:
        own, school = await _two_tenants(session_factory, seed)

        report = await _funnel(session_factory, "tenant")

        rows = {r.group.tenant_id: r for r in report.rows}
        assert set(rows) == {own.tenant_id, school.tenant_id}
        assert rows[school.tenant_id].group.tenant_name == school.tenant_name
        assert rows[school.tenant_id].purchased_money == (
            ("EUR", 100),
            ("USD", 2000),
        )
        assert rows[own.tenant_id].purchased_money == (("EUR", 1500),)

    async def test_by_course_counts_a_student_in_each_and_carries_no_money(
        self, session_factory: Factory, seed: Seed
    ) -> None:
        own, school = await _two_tenants(session_factory, seed)

        report = await _funnel(session_factory, "course")

        rows = {r.group.course_id: r for r in report.rows}
        assert _figures(rows[school.courses["Data"][0]]) == (2, 0, 1, 1)
        assert _figures(rows[school.courses["Web"][0]]) == (1, 1, 1, 1)
        assert _figures(rows[own.courses["Python"][0]]) == (3, 2, 1, 1)
        assert rows[school.courses["Web"][0]].group.course_title == "Web"
        assert {r.purchased_money for r in report.rows} == {None}
        assert report.total.purchased_money is None

    async def test_by_source(self, session_factory: Factory, seed: Seed) -> None:
        await _two_tenants(session_factory, seed)

        report = await _funnel(session_factory, "source")

        rows = {(r.group.source, r.group.source_tag): r for r in report.rows}
        assert list(rows) == [
            ("author", None),
            ("self", "fb-dec-ads"),
            ("self", "ig-x"),
        ]
        assert _figures(rows[("author", None)]) == (1, 0, 0, 0)
        assert _figures(rows[("self", "fb-dec-ads")]) == (3, 2, 1, 1)
        assert _figures(rows[("self", "ig-x")]) == (2, 1, 2, 2)
        assert {r.purchased_money for r in report.rows} == {None}

    async def test_the_command_prints_text_csv_and_json(
        self, session_factory: Factory, seed: Seed
    ) -> None:
        await _two_tenants(session_factory, seed)
        base = ["funnel", "--from", "2025-03-01", "--to", "2025-03-31", "--by"]

        text_out = await _run([*base, "path"])
        csv_out = await _run([*base, "course", "--format", "csv"])
        json_out = json.loads(await _run([*base, "source", "--format", "json"]))

        lines = text_out.splitlines()
        assert (
            lines[0] == "Funnel (UTC): 2025-03-01 .. 2025-03-31 (inclusive) | by: path"
        )
        assert lines[1].split() == [
            "path",
            "enrolled",
            "first_submission",
            "first_purchase",
            "repeat_purchase",
            "purchased_money",
        ]
        assert lines[3].split() == ["own_course", "3", "2", "1", "1", "EUR", "1500"]
        assert lines[4].split() == [
            "school",
            "3",
            "1",
            "2",
            "2",
            "EUR",
            "100;",
            "USD",
            "2000",
        ]
        assert lines[6].split() == [
            "total",
            "6",
            "3",
            "3",
            "3",
            "EUR",
            "1600;",
            "USD",
            "2000",
        ]
        assert lines[7] == "excluded: 1 test students"

        rows = list(csv.DictReader(io.StringIO(csv_out)))
        assert {r["purchased_money"] for r in rows} == {""}
        assert rows[-1]["row_kind"] == "total"
        assert rows[-1]["enrolled"] == "6"
        assert rows[-1]["note"] == "excluded: 1 test students"
        assert {r["course_title"] for r in rows[:-1]} == {"Data", "Web", "Python"}

        assert json_out["excluded_test_students"] == 1
        assert json_out["rows"][0] == {
            "source": "author",
            "source_tag": None,
            "enrolled": 1,
            "first_submission": 0,
            "first_purchase": 0,
            "repeat_purchase": 0,
            "purchased_money": None,
        }
        assert json_out["total"]["enrolled"] == 6

    async def test_the_command_runs_read_only(
        self, session_factory: Factory, seed: Seed
    ) -> None:
        async with session_factory() as session, session.begin():
            args = billing_admin.build_parser().parse_args(
                ["funnel", "--from", "2025-03-01", "--to", "2025-03-31"]
            )
            await billing_admin.funnel_cmd(session, args)
            read_only = await session.scalar(text("SHOW transaction_read_only"))
        assert read_only == "on"
