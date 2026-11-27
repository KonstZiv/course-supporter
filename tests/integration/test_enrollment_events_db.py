"""Б-4: the join link's campaign tag and the enrollment event, on a live database.

Locks of the task:

* (б) a signup with a valid ``source_tag`` keeps it on the signup and on the
  enrollment event of its confirm; an invalid one (``"Bad Tag!"``, 41
  characters, Cyrillic) does not refuse — the student is enrolled, the tag is
  null in both places, and an INFO ``signup_source_tag_ignored`` without the
  value is logged. A join by a student with a login carries the tag too.
* (в) an author's grant and a self-enrollment write the event in the same
  transaction as the grant: rolled back — neither the grant nor the event.
* (г) the event outlives the grant: withdrawn by the author, or the student
  deleted (grant row deleted, student soft-deleted) — the funnel still counts
  the event.

Requires ``docker compose up -d`` (PostgreSQL), migrated to head; ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.billing.funnel import FunnelRow, build_funnel
from course_supporter.open_courses.service import (
    SOURCE_SELF,
    confirm_signup,
    find_open_course,
    join_course,
)
from course_supporter.storage.orm import (
    EnrollmentEvent,
    PortalSignup,
    Student,
    StudentEnrollment,
)
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)
from tests._helpers.open_courses import (
    TEXT_SHA,
    FakeArq,
    World,
    add_login,
    app_client,
    auth,
    confirm,
    drop_world,
    enroll_by_link,
    make_world,
    open_link,
    portal_token,
    provision,
    signup,
    terms,
)

pytestmark = pytest.mark.requires_db

Factory = async_sessionmaker[AsyncSession]
IGNORED = "signup_source_tag_ignored"


@pytest.fixture()
async def world(session_factory: Factory) -> AsyncIterator[World]:
    made = await make_world(session_factory)
    yield made
    await drop_world(session_factory, made)


@pytest.fixture()
def arq() -> FakeArq:
    return FakeArq()


@pytest.fixture()
async def client(
    world: World, session_factory: Factory, arq: FakeArq
) -> AsyncIterator[AsyncClient]:
    async with app_client(world, session_factory, arq) as http:
        yield http


async def _events(factory: Factory, student_id: uuid.UUID) -> list[EnrollmentEvent]:
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(EnrollmentEvent).where(
                        EnrollmentEvent.student_id == student_id
                    )
                )
            ).all()
        )


async def _count(factory: Factory, model: type, tenant_course: uuid.UUID) -> int:
    """Rows of ``model`` (an enrollment or an event) in the course."""
    async with factory() as session:
        column = model.course_node_id  # type: ignore[attr-defined]
        return int(
            await session.scalar(
                select(func.count()).select_from(model).where(column == tenant_course)
            )
            or 0
        )


async def _signup_tag(factory: Factory, tenant_id: uuid.UUID, login: str) -> str | None:
    async with factory() as session:
        return await session.scalar(
            select(PortalSignup.source_tag).where(
                PortalSignup.tenant_id == tenant_id, PortalSignup.login == login
            )
        )


# ── (б) The campaign tag of a signup ───────────────────────────────────────


class TestSourceTag:
    async def test_a_valid_tag_is_on_the_signup_and_on_the_event(
        self, client: AsyncClient, world: World, arq: FakeArq, session_factory: Factory
    ) -> None:
        key = await open_link(client, world.course)
        done = await enroll_by_link(
            client,
            arq,
            world.tenant_a,
            key,
            "tagged",
            "t@example.com",
            source_tag="fb-dec-ads",
        )

        assert await _signup_tag(session_factory, world.tenant_a, "tagged") == (
            "fb-dec-ads"
        )
        events = await _events(session_factory, uuid.UUID(done["student_id"]))
        assert [(e.source, e.source_tag, e.course_node_id) for e in events] == [
            (SOURCE_SELF, "fb-dec-ads", world.course)
        ]
        assert events[0].tenant_id == world.tenant_a

    async def test_no_tag_is_null(
        self, client: AsyncClient, world: World, arq: FakeArq, session_factory: Factory
    ) -> None:
        key = await open_link(client, world.course)
        done = await enroll_by_link(client, arq, world.tenant_a, key, "plain", "p@x.io")

        assert await _signup_tag(session_factory, world.tenant_a, "plain") is None
        events = await _events(session_factory, uuid.UUID(done["student_id"]))
        assert [e.source_tag for e in events] == [None]

    @pytest.mark.parametrize("bad", ["Bad Tag!", "a" * 41, "реклама", "", 42], ids=str)
    async def test_an_invalid_tag_enrolls_with_null_and_a_log_line_without_it(
        self,
        client: AsyncClient,
        world: World,
        arq: FakeArq,
        session_factory: Factory,
        bad: object,
    ) -> None:
        key = await open_link(client, world.course)
        with capture_logs() as logs:
            resp = await signup(
                client, world.tenant_a, key, "odd", "o@example.com", source_tag=bad
            )
        assert resp.status_code == 202, resp.text
        done = await confirm(client, arq.last_token())
        assert done.status_code == 200, done.text

        assert await _signup_tag(session_factory, world.tenant_a, "odd") is None
        events = await _events(session_factory, uuid.UUID(done.json()["student_id"]))
        assert [e.source_tag for e in events] == [None]
        ignored = [e for e in logs if e["event"] == IGNORED]
        assert len(ignored) == 1
        assert ignored[0] == {"event": IGNORED, "log_level": "info"}, "no value"

    async def test_a_join_by_a_student_with_a_login_carries_the_tag(
        self, client: AsyncClient, world: World, session_factory: Factory
    ) -> None:
        key = await open_link(client, world.course)
        student_id = await add_login(
            session_factory, world.tenant_a, "veteran", email="v@example.com"
        )
        token = await portal_token(client, world.tenant_a, "veteran")
        resp = await client.post(
            "/api/v1/portal/courses/join",
            json={
                "tenant_id": str(world.tenant_a),
                "key": key,
                "adult": True,
                "terms_version": terms(),
                "text_sha256": TEXT_SHA,
                "source_tag": "site_pricing",
            },
            headers=auth(token),
        )
        assert resp.status_code == 201, resp.text

        events = await _events(session_factory, student_id)
        assert [(e.source, e.source_tag) for e in events] == [
            (SOURCE_SELF, "site_pricing")
        ]


# ── (в) The event is written with the grant, in its transaction ────────────


class TestSameTransaction:
    async def test_an_authors_grant_writes_an_author_event(
        self, client: AsyncClient, world: World, session_factory: Factory
    ) -> None:
        student_id = uuid.UUID(await provision(client, "granted"))
        resp = await client.post(
            "/api/v1/students/enrollments",
            json={"student_id": str(student_id), "course_node_id": str(world.course)},
        )
        assert resp.status_code == 201, resp.text

        events = await _events(session_factory, student_id)
        assert [(e.source, e.source_tag, e.tenant_id) for e in events] == [
            ("author", None, world.tenant_a)
        ]
        async with session_factory() as session:
            granted = await session.scalar(
                select(StudentEnrollment.created_at).where(
                    StudentEnrollment.student_id == student_id
                )
            )
        assert events[0].created_at == granted, "one transaction, one now()"

    async def test_a_rolled_back_authors_grant_leaves_neither(
        self, world: World, session_factory: Factory
    ) -> None:
        async with session_factory() as session:
            student = Student(tenant_id=world.tenant_a, external_id="rolled-back")
            session.add(student)
            await session.commit()
            student_id = student.id
        async with session_factory() as session:
            await StudentEnrollmentRepository(session).bind(
                tenant_id=world.tenant_a,
                student_id=student_id,
                course_node_id=world.course,
                source="author",
            )
            await session.rollback()

        assert await _count(session_factory, StudentEnrollment, world.course) == 0
        assert await _count(session_factory, EnrollmentEvent, world.course) == 0

    async def test_a_self_enrollment_writes_the_event_and_a_rollback_leaves_neither(
        self, client: AsyncClient, world: World, arq: FakeArq, session_factory: Factory
    ) -> None:
        key = await open_link(client, world.course)
        resp = await signup(
            client, world.tenant_a, key, "selfie", "s@example.com", source_tag="ads"
        )
        assert resp.status_code == 202, resp.text
        token = arq.last_token()

        async with session_factory() as session:
            done = await confirm_signup(session, raw_token=token, now=datetime.now(UTC))
            in_transaction = (
                await session.scalars(
                    select(EnrollmentEvent.source_tag).where(
                        EnrollmentEvent.student_id == done.student.id
                    )
                )
            ).all()
            await session.rollback()
        assert in_transaction == ["ads"], "written by the confirm itself"
        assert await _count(session_factory, StudentEnrollment, world.course) == 0
        assert await _count(session_factory, EnrollmentEvent, world.course) == 0

        committed = await confirm(client, token)
        assert committed.status_code == 200, committed.text
        assert await _count(session_factory, StudentEnrollment, world.course) == 1
        assert await _count(session_factory, EnrollmentEvent, world.course) == 1

    async def test_a_join_that_rolls_back_leaves_neither(
        self, client: AsyncClient, world: World, session_factory: Factory
    ) -> None:
        key = await open_link(client, world.course)
        student_id = await add_login(
            session_factory, world.tenant_a, "joiner", email="j@example.com"
        )
        async with session_factory() as session:
            found = await find_open_course(session, tenant_id=world.tenant_a, key=key)
            assert found is not None
            await join_course(
                session,
                student_id=student_id,
                open_course=found,
                terms_version=terms(),
                current_version=terms(),
                text_sha256=TEXT_SHA,
                now=datetime.now(UTC),
                source_tag="x",
            )
            assert (
                len(
                    (
                        await session.scalars(
                            select(EnrollmentEvent.id).where(
                                EnrollmentEvent.student_id == student_id
                            )
                        )
                    ).all()
                )
                == 1
            )
            await session.rollback()

        assert await _count(session_factory, StudentEnrollment, world.course) == 0
        assert await _count(session_factory, EnrollmentEvent, world.course) == 0


# ── (г) The event outlives the grant ───────────────────────────────────────


def _row_of(rows: tuple[FunnelRow, ...], tenant_id: uuid.UUID) -> FunnelRow:
    return next(r for r in rows if r.group.tenant_id == tenant_id)


class TestOutlivesTheGrant:
    async def test_withdrawn_and_deleted_students_still_count_and_reenrolment_is_new(
        self, client: AsyncClient, world: World, arq: FakeArq, session_factory: Factory
    ) -> None:
        key = await open_link(client, world.course)
        gone = uuid.UUID(
            (await enroll_by_link(client, arq, world.tenant_a, key, "gone", "g@x.io"))[
                "student_id"
            ]
        )
        back = uuid.UUID(await provision(client, "back"))
        grant = {"student_id": str(back), "course_node_id": str(world.course)}
        assert (
            await client.post("/api/v1/students/enrollments", json=grant)
        ).is_success
        # Withdrawn by the author, then granted again: a second event.
        assert (
            await client.delete("/api/v1/students/enrollments", params=grant)
        ).is_success
        assert (
            await client.post("/api/v1/students/enrollments", json=grant)
        ).is_success
        # The deletion on request: the grant row goes, the student stays
        # soft-deleted (student_admin.py delete, Б-2).
        async with session_factory() as session:
            await session.execute(
                delete(StudentEnrollment).where(StudentEnrollment.student_id == gone)
            )
            await session.execute(
                update(Student)
                .where(Student.id == gone)
                .values(deleted_at=datetime.now(UTC), external_id=f"deleted-{gone}")
            )
            await session.commit()

        assert await _count(session_factory, StudentEnrollment, world.course) == 1
        assert len(await _events(session_factory, gone)) == 1
        assert len(await _events(session_factory, back)) == 2
        today = datetime.now(UTC).date()
        async with session_factory() as session:
            report = await build_funnel(
                session, from_date=today, to_date=today, by="tenant"
            )
        assert _row_of(report.rows, world.tenant_a).enrolled == 3
