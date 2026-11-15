"""BIL-E2 part C: the author's spending in units, by course and task, on a live DB.

The world: one tenant with two courses. Course 1 has a homework task with two
reviews of a student's work (the author pays under a course rule) and a
material processed once; course 2 has a task with one review the platform
bore and a preparation. A review whose submission is gone is ``unlinked``.
Movements are written by the journal itself (``ledger.settle`` with the payer)
at fixed instants inside October 2026, so the period is exact whatever day
the test runs on.

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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.billing import charging, ledger
from course_supporter.billing.accounts import (
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.kinds import ActionKind, PayerSource
from course_supporter.billing.links import UNLINKED, ActionRef, Link, link_actions
from course_supporter.cost_estimate.shadow import preparation_key
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

OCTOBER = {"from": "2026-10-01", "to": "2026-10-31"}
IN_OCTOBER = datetime(2026, 10, 15, 12, tzinfo=UTC)
BOUGHT = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    course_1: uuid.UUID
    course_2: uuid.UUID
    task_1: uuid.UUID
    task_2: uuid.UUID
    material: uuid.UUID
    module: uuid.UUID
    submissions: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
    processing_job: uuid.UUID
    summary_job: uuid.UUID
    author: uuid.UUID
    student: uuid.UUID


async def _make_world(session_factory: async_sessionmaker[AsyncSession]) -> World:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-e2-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        course_1 = make_root_course_node(tenant_id=tenant.id, title="Курс 1", order=0)
        course_2 = make_root_course_node(tenant_id=tenant.id, title="Курс 2", order=1)
        session.add_all([course_1, course_2])
        await session.flush()
        module = CourseNode(
            tenant_id=tenant.id, parent_id=course_1.id, title="Модуль", order=0
        )
        session.add(module)
        await session.flush()

        def doc(course: CourseNode, *, title: str, task_type: str | None) -> Any:
            return AuthoredDocument(
                course_node_id=course.id,
                course_root_id=course.id,
                source_type="text",
                source_url=f"https://example.com/{uuid.uuid4().hex}",
                title=title,
                task_type=task_type,
            )

        task_1 = doc(course_1, title="Завдання 1", task_type="task")
        task_2 = doc(course_2, title="Завдання 2", task_type="task")
        material = doc(course_1, title="Лекція", task_type=None)
        student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:8]}")
        session.add_all([task_1, task_2, material, student])
        await session.flush()
        submissions = [
            HomeworkSubmission(
                tenant_id=tenant.id,
                student_id=student.id,
                course_node_id=task.course_root_id,
                node_id=task.course_root_id,
                authored_document_id=task.id,
                file_url="s3://bucket/work.py",
                file_type="text/plain",
                original_filename="work.py",
                status="completed",
            )
            for task in (task_1, task_1, task_2)
        ]
        session.add_all(submissions)
        processing = Job(
            tenant_id=tenant.id,
            course_node_id=course_1.id,
            job_type=JobType.DOCUMENT_PROCESSING.value,
            subject_type=JOB_SUBJECT_TYPE[JobType.DOCUMENT_PROCESSING],
            subject_id=material.id,
            status="complete",
        )
        summary = Job(
            tenant_id=tenant.id,
            course_node_id=module.id,
            job_type=JobType.NODE_SUMMARY_REGENERATION.value,
            subject_type=JOB_SUBJECT_TYPE[JobType.NODE_SUMMARY_REGENERATION],
            subject_id=module.id,
            status="complete",
        )
        session.add_all([processing, summary])
        await session.flush()
        author = await get_or_create_author_account(session, tenant_id=tenant.id)
        student_account = await get_or_create_student_account(
            session, tenant_id=tenant.id, student_id=student.id
        )
        await session.commit()
        return World(
            tenant.id,
            course_1.id,
            course_2.id,
            task_1.id,
            task_2.id,
            material.id,
            module.id,
            (submissions[0].id, submissions[1].id, submissions[2].id),
            processing.id,
            summary.id,
            author.id,
            student_account.id,
        )


@pytest.fixture()
async def worlds(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[World, World]]:
    made = (await _make_world(session_factory), await _make_world(session_factory))
    yield made
    for world in made:
        await drop_billing_of_tenant(session_factory, world.tenant_id)


async def _settle(
    session_factory: async_sessionmaker[AsyncSession],
    world: World,
    kind: ActionKind,
    key: str,
    units: int,
    *,
    at: datetime = IN_OCTOBER,
    account: uuid.UUID | None = None,
    to_platform: bool = False,
) -> None:
    async with session_factory() as session:
        await ledger.settle(
            session,
            action_kind=kind,
            action_key=key,
            fact_units=units,
            beneficiary_account_id=world.student
            if kind is ActionKind.REVIEW
            else world.author,
            payer_source=PayerSource.COURSE_RULE
            if kind is ActionKind.REVIEW
            else PayerSource.DEFAULT,
            account_id=account or world.author,
            excess_to_platform=to_platform,
            now=at,
        )
        await session.commit()


async def _spend(session_factory: async_sessionmaker[AsyncSession], w: World) -> None:
    """The world's actions, charged to its author in October."""
    async with session_factory() as session:
        for account in (w.author, w.student):
            await ledger.credit_purchase(
                session,
                account_id=account,
                units=1000,
                amount_paid_minor=500,
                currency="EUR",
                provider="fake",
                now=BOUGHT,
            )
        await session.commit()
    s1, s2, s3 = w.submissions
    review, processing = ActionKind.REVIEW, ActionKind.PROCESSING
    prep = ActionKind.TASK_PREPARATION
    await _settle(session_factory, w, review, str(s1), 100)
    await _settle(session_factory, w, review, str(s2), 50)
    await _settle(session_factory, w, processing, str(w.processing_job), 200)
    await _settle(session_factory, w, review, str(s3), 30, to_platform=True)
    await _settle(
        session_factory,
        w,
        prep,
        preparation_key(w.task_2, "v1", "criteria_decomposition"),
        40,
    )
    await _settle(session_factory, w, review, str(uuid.uuid4()), 7)  # no submission
    # Not the author's spending: the student's own review, and the period's edges.
    await _settle(session_factory, w, review, str(uuid.uuid4()), 9, account=w.student)
    await _settle(
        session_factory,
        w,
        review,
        str(uuid.uuid4()),
        11,
        at=datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC),
    )
    await _settle(
        session_factory,
        w,
        review,
        str(uuid.uuid4()),
        13,
        at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    await _settle(
        session_factory,
        w,
        review,
        str(uuid.uuid4()),
        3,
        at=datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC),
    )


@pytest.fixture()
def as_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    worlds: tuple[World, World],
) -> Iterator[None]:
    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
        tenant_id=worlds[0].tenant_id,
        tenant_name="t",
        scopes=["prep"],
        plan_id="basic",
        key_prefix="cs_test",
    )
    yield
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _get(params: dict[str, str] | None = None) -> Any:
    async with _client() as client:
        return await client.get("/api/v1/billing/spending", params=params)


@pytest.mark.usefixtures("as_tenant")
class TestSpending:
    async def test_the_sums_and_the_breakdown(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        worlds: tuple[World, World],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: True)
        w, other = worlds
        for world in worlds:
            await _spend(session_factory, world)

        response = await _get(OCTOBER)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["period"] == {"from": "2026-10-01", "to": "2026-10-31"}
        assert body["charging_enabled"] is True
        assert body["totals"] == {"billed_units": 400, "covered_units": 30}, (
            "100 + 50 + 200 + 40 + 7 (unlinked) + 3 (the last second); 30 the "
            "platform's; the student's own and the edges outside are not"
        )
        assert body["unlinked"] == {"billed_units": 10, "covered_units": 0}
        first, second = body["courses"]
        assert first == {
            "course_node_id": str(w.course_1),
            "title": "Курс 1",
            "billed_units": 350,
            "covered_units": 0,
            "reviews_units": 150,
            "processing_units": 200,
            "preparation_units": 0,
            "tasks": [
                {
                    "authored_document_id": str(w.task_1),
                    "title": "Завдання 1",
                    "reviews": 2,
                    "billed_units": 150,
                    "covered_units": 0,
                }
            ],
        }
        assert second == {
            "course_node_id": str(w.course_2),
            "title": "Курс 2",
            "billed_units": 40,
            "covered_units": 30,
            "reviews_units": 0,
            "processing_units": 0,
            "preparation_units": 40,
            "tasks": [
                {
                    "authored_document_id": str(w.task_2),
                    "title": "Завдання 2",
                    "reviews": 1,
                    "billed_units": 40,
                    "covered_units": 30,
                }
            ],
        }
        assert str(other.course_1) not in response.text, "another tenant's: unseen"

    async def test_a_period_without_spending_is_zeros(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        worlds: tuple[World, World],
    ) -> None:
        await _spend(session_factory, worlds[0])

        body = (await _get({"from": "2026-12-01", "to": "2026-12-31"})).json()

        assert body["totals"] == {"billed_units": 0, "covered_units": 0}
        assert body["courses"] == []

    async def test_charging_off_says_so_and_spends_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: False)

        body = (await _get()).json()

        assert body["charging_enabled"] is False
        assert body["totals"] == {"billed_units": 0, "covered_units": 0}
        assert body["unlinked"] == {"billed_units": 0, "covered_units": 0}
        assert body["courses"] == []
        today = datetime.now(UTC).date()
        assert body["period"] == {
            "from": today.replace(day=1).isoformat(),
            "to": today.isoformat(),
        }, "the current UTC month by default"

    async def test_a_period_backwards_is_refused(self) -> None:
        response = await _get({"from": "2026-10-31", "to": "2026-10-01"})

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_period"

    async def test_no_dollars_in_the_answer(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        worlds: tuple[World, World],
    ) -> None:
        """The cost in dollars is the operator's (5b): a lock on the answer and
        on its published schema."""
        await _spend(session_factory, worlds[0])
        body = (await _get(OCTOBER)).json()
        schema = app.openapi()["components"]["schemas"]
        published = {
            name
            for model, spec in schema.items()
            if model.startswith("BillingSpending")
            for name in spec.get("properties", {})
        }

        def keys(value: Any) -> set[str]:
            if isinstance(value, dict):
                return set(value) | {k for v in value.values() for k in keys(v)}
            if isinstance(value, list):
                return {k for v in value for k in keys(v)}
            return set()

        for name in keys(body) | published:
            assert not name.endswith("_usd"), name
            assert "cost" not in name, name


class TestLinks:
    async def test_every_kind_leads_to_its_course_and_task(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        worlds: tuple[World, World],
    ) -> None:
        w = worlds[0]
        refs = {
            "review": ActionRef(ActionKind.REVIEW, str(w.submissions[2])),
            "prep": ActionRef(
                ActionKind.TASK_PREPARATION,
                preparation_key(w.task_1, "v", "key_explanation"),
            ),
            "prep_by_task": ActionRef(ActionKind.TASK_PREPARATION, str(w.task_2)),
            "processing": ActionRef(ActionKind.PROCESSING, str(w.processing_job)),
            "summary": ActionRef(ActionKind.PROCESSING, str(w.summary_job)),
            "gone": ActionRef(ActionKind.REVIEW, str(uuid.uuid4())),
            "malformed": ActionRef(ActionKind.PROCESSING, "not-a-key"),
        }
        async with session_factory() as session:
            found = await link_actions(session, refs.values())

        assert found[refs["review"]] == Link(w.course_2, w.task_2)
        assert found[refs["prep"]] == Link(w.course_1, w.task_1)
        assert found[refs["prep_by_task"]] == Link(w.course_2, w.task_2)
        assert found[refs["processing"]] == Link(w.course_1, w.material)
        assert found[refs["summary"]] == Link(w.course_1, None), (
            "a job about a module: the module's course"
        )
        assert found[refs["gone"]] == UNLINKED
        assert found[refs["malformed"]] == UNLINKED


# ── Part D: the administrator's cost report, dollars beside units ─────


async def _calls(session_factory: async_sessionmaker[AsyncSession], w: World) -> None:
    """The world's paid calls in October: reviews, a preparation inside a review,
    the processing, the key explanations of task 2."""
    from decimal import Decimal

    from course_supporter.cost_kinds import CostKind
    from course_supporter.storage.orm import ExternalServiceCall

    async with session_factory() as session:
        s1, _, s3 = w.submissions
        jobs = {}
        for name, job_type, subject in (
            ("s1", JobType.HOMEWORK_PROCESSING, s1),
            ("s3", JobType.HOMEWORK_PROCESSING, s3),
            ("key", JobType.KEY_EXPLANATION, w.task_2),
        ):
            job = Job(
                tenant_id=w.tenant_id,
                course_node_id=w.course_1,
                job_type=job_type.value,
                subject_type=JOB_SUBJECT_TYPE[job_type],
                subject_id=subject,
                status="complete",
            )
            session.add(job)
            await session.flush()
            jobs[name] = job.id
        jobs["processing"] = w.processing_job
        for job_name, action, usd in (
            ("s1", "criteria_evaluation", "0.10"),
            ("s1", "criteria_decomposition", "0.02"),
            ("s3", "criteria_evaluation", "0.05"),
            ("processing", "transcription", "0.20"),
            ("key", "key_explanation", "0.03"),
        ):
            session.add(
                ExternalServiceCall(
                    job_id=jobs[job_name],
                    action=action,
                    provider="gemini",
                    model_id="gemini-2.5-flash",
                    success=True,
                    unit_type="tokens",
                    unit_in=1,
                    unit_out=1,
                    cost_usd=Decimal(usd),
                    cost_kind=CostKind.ESTIMATED.value,
                    call_started_at=IN_OCTOBER,
                    created_at=IN_OCTOBER,
                )
            )
        await session.commit()


class TestCostReport:
    async def test_by_task_dollars_and_units_match_the_data(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        worlds: tuple[World, World],
    ) -> None:
        from decimal import Decimal

        from scripts import billing_admin

        from course_supporter.billing import costs

        w = worlds[0]
        await _spend(session_factory, w)
        await _calls(session_factory, w)
        async with session_factory() as session:
            by_task = await costs.build_cost_report(
                session,
                from_date=IN_OCTOBER.date().replace(day=1),
                to_date=IN_OCTOBER.date().replace(day=31),
                tenant_id=w.tenant_id,
                by="task",
            )
            both = await costs.build_cost_report(
                session,
                from_date=IN_OCTOBER.date().replace(day=1),
                to_date=IN_OCTOBER.date().replace(day=31),
                tenant_id=w.tenant_id,
                by="both",
            )

        rows = {
            (r.course_node_id, r.authored_document_id): (
                r.spend.known_usd,
                r.units.billed_units,
                r.units.covered_units,
            )
            for r in by_task.tasks
        }
        assert rows == {
            (w.course_1, w.task_1): (Decimal("0.12"), 150, 0),
            (w.course_1, None): (Decimal("0.20"), 200, 0),
            (w.course_2, w.task_2): (Decimal("0.08"), 40, 30),
            (None, None): (Decimal(0), 19, 0),
        }, (
            "a preparation inside a review is its task's; processing the course's; "
            "no course: the author's 7 + 3 and the student's own 9 (its key leads "
            "to no submission) — every payer of the tenant, as the author row"
        )
        assert (
            by_task.tasks_total.spend.known_usd,
            by_task.tasks_total.units.billed_units,
            by_task.tasks_total.units.covered_units,
        ) == (Decimal("0.40"), 409, 30)

        (author,) = both.authors
        assert (author.spend.known_usd, author.units) == (
            Decimal("0.40"),
            costs.UnitsSummary(billed_units=409, covered_units=30),
        ), "the tenant's accounts: the author's 400 and the student's own 9"
        (student,) = both.students
        assert (student.spend.known_usd, student.units) == (
            Decimal("0.15"),
            costs.UnitsSummary(billed_units=169, covered_units=30),
        ), "the reviews for the student, whoever paid: 100 + 50 + 7 + 3 + 9; 30"

        table = billing_admin._render_cost_table(by_task, tenant_label=None)
        assert "По завданнях (автор → курс → завдання):" in table
        assert "Завдання 1" in table
        assert billing_admin.NO_TASK in table
        assert billing_admin.NO_COURSE in table
        csv_rows = [
            line.split(",")
            for line in billing_admin._render_cost_csv(by_task).splitlines()
        ]
        header = csv_rows[0]
        total = next(r for r in csv_rows if r[0] == "task" and r[1] == "total")
        assert total[header.index("billed_units")] == "409"
        assert total[header.index("covered_units")] == "30"
        assert Decimal(total[header.index("total_usd")]) == Decimal("0.40")
