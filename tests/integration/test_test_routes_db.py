"""The four routes of a test, over HTTP with a live database (task 07).

What a student and a channel see of a test before answering it, and how the
answers come in. One class per lock:

- the structure's fields, listed — three at the top, three in a question, two
  in an option — and nothing of the key at any depth;
- tenant isolation: another tenant's task answers exactly as a missing one,
  byte for byte, on all four routes — on the portal's structure route even to
  a student enrolled in its course, a state only a direct write reaches;
- the doors' codes reach both entries as ``{"code", "details"}``;
- the channel's two routes want the ``CHECK`` scope, as its file route does;
- the portal tree's ``test_form``: a test's only, and only while tests are on
  the new path.

And three for what the routes' docstrings claim: a deleted task is a missing
one on all four routes; answers come in as a submission of their entry; the
gates the routes share with the file routes refuse with the file routes' own
bytes.

The storage and the queue are doubles; the database is real.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_arq_redis,
    get_current_student,
    get_current_tenant,
    get_s3_client,
)
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    SubmissionState,
)
from course_supporter.homework.test_text import canonical_answers_json
from course_supporter.reference_kinds import ReferenceKind
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    HomeworkSubmission,
    Job,
    Student,
    StudentEnrollment,
    TaskReferenceOverride,
    Tenant,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_TEXT = (
    "1. Перше?\nа) так\nб) ні\n\n2. Друге?\nа) так\nв) ні\n\n3. Третє?\nа) так\nб) ні"
)
_HASH = "e2" + "0" * 62
_KEY: dict[str, list[str]] = {"1": ["б"], "2": ["в"], "3": ["а"]}
_ANSWERS: dict[str, list[str]] = {"1": ["б"], "2": ["а"], "3": ["а"]}
_SWITCH = "course_supporter.homework.test_doors.get_path_config"

_ROUTES = ["portal-structure", "portal-submit", "channel-structure", "channel-submit"]
_TOP = {"version", "accepting_answers", "questions"}
_QUESTION = {"number", "text", "options"}
_OPTION = {"label", "text"}
_OF_THE_KEY = {
    "answers",
    "key",
    "correct",
    "correct_answer",
    "explanation",
    "explanations",
    "author",
    "model",
    "doubts",
    "pass_threshold",
}


def _config(*, test_on_new_path: bool) -> PathConfig:
    """The shipped shape, with the ``test`` switch where the test wants it."""
    served = ServedBy.NEW_PATH if test_on_new_path else ServedBy.TODAYS_MENTOR
    stage = {
        "deterministic": True,
        "prompt_ref": "prompts/safety_check/v1.md",
        "requires": [],
        "input_budget_ratio": None,
        "record_output": False,
        "ceilings": {"tool_steps": 0, "money_usd": 0.05, "output_tokens": 8192},
        "ladder": [
            {
                "provider": "mistral",
                "model": "m",
                "reasoning": None,
                "max_output_tokens": None,
            }
        ],
    }
    return PathConfig.model_validate(
        {
            "stages": {"safety": stage},
            "task_types": {
                "test": {
                    "served_by": served.value,
                    "paths": {s.value: [] for s in SubmissionState},
                },
                "short_task": {"served_by": "todays_mentor", "paths": {}},
                "task": {"served_by": "todays_mentor", "paths": {}},
                "project": {"served_by": "todays_mentor", "paths": {}},
            },
        }
    )


@dataclass(frozen=True)
class World:
    """Two tenants: the owner of the course and a stranger with a course of its own."""

    tenant: uuid.UUID
    course: uuid.UUID
    child: uuid.UUID
    test: uuid.UUID
    plain: uuid.UUID
    material: uuid.UUID
    hidden: uuid.UUID
    student: uuid.UUID
    outsider: uuid.UUID
    stranger: uuid.UUID
    stranger_course: uuid.UUID
    stranger_student: uuid.UUID


async def _document(
    session: AsyncSession,
    node: CourseNode,
    *,
    task_type: str | None,
    material_role: str = "educational",
) -> AuthoredDocument:
    """A ready document with the test's text as its one segment."""
    document = AuthoredDocument(
        course_node_id=node.id,
        course_root_id=node.id,
        source_type="text",
        source_url="https://example.test/task",
        task_type=task_type,
        material_role=material_role,
        language="ukr",
        content_hash=_HASH,
    )
    session.add(document)
    await session.flush()
    summary = DocumentSummary(
        authored_document_id=document.id,
        course_root_id=node.id,
        title="Тест",
        status="ready",
    )
    session.add(summary)
    await session.flush()
    session.add(
        DocumentSegment(
            document_summary_id=summary.id,
            course_root_id=node.id,
            order=0,
            content=_TEXT,
            description="the test",
            start_pos=0,
            end_pos=len(_TEXT),
        )
    )
    await session.flush()
    return document


async def _enrolled(
    session: AsyncSession, tenant: Tenant, course: CourseNode | None
) -> Student:
    """A student of the tenant, enrolled in ``course`` unless it is None."""
    student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:6]}")
    session.add(student)
    await session.flush()
    if course is not None:
        session.add(StudentEnrollment(student_id=student.id, course_node_id=course.id))
        await session.flush()
    return student


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[World]:
    """The owner's course with a test and its key; the stranger's own course."""
    async with session_factory() as session:
        tenant = Tenant(name=f"routes-{uuid.uuid4().hex[:8]}")
        stranger = Tenant(name=f"stranger-{uuid.uuid4().hex[:8]}")
        session.add_all([tenant, stranger])
        await session.flush()
        course = make_root_course_node(tenant_id=tenant.id, title="Routes", order=0)
        stranger_course = make_root_course_node(
            tenant_id=stranger.id, title="Elsewhere", order=0
        )
        session.add_all([course, stranger_course])
        await session.flush()
        child = CourseNode(
            tenant_id=tenant.id, parent_id=course.id, title="Section", order=0
        )
        session.add(child)
        await session.flush()
        test = await _document(session, course, task_type="test")
        plain = await _document(session, course, task_type="task")
        material = await _document(session, course, task_type=None)
        hidden = await _document(
            session, course, task_type="test", material_role="methodological"
        )
        await TaskReferenceRepository(session).replace_override(
            authored_document_id=test.id,
            kind=ReferenceKind.TEST_KEY,
            answers=_KEY,
            source_content_hash=_HASH,
        )
        student = await _enrolled(session, tenant, course)
        outsider = await _enrolled(session, tenant, None)
        stranger_student = await _enrolled(session, stranger, stranger_course)
        await session.commit()
        built = World(
            tenant=tenant.id,
            course=course.id,
            child=child.id,
            test=test.id,
            plain=plain.id,
            material=material.id,
            hidden=hidden.id,
            student=student.id,
            outsider=outsider.id,
            stranger=stranger.id,
            stranger_course=stranger_course.id,
            stranger_student=stranger_student.id,
        )

    yield built

    async with session_factory() as session:
        for tenant_id in (built.tenant, built.stranger):
            await session.execute(
                delete(HomeworkSubmission).where(
                    HomeworkSubmission.tenant_id == tenant_id
                )
            )
            await session.execute(delete(Job).where(Job.tenant_id == tenant_id))
            students = select(Student.id).where(Student.tenant_id == tenant_id)
            await session.execute(
                delete(StudentEnrollment).where(
                    StudentEnrollment.student_id.in_(students)
                )
            )
            await session.execute(delete(Student).where(Student.tenant_id == tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


def _act_as(
    *, tenant: uuid.UUID, student: uuid.UUID, scopes: list[str] | None = None
) -> None:
    """Who calls: a channel's key and a student's session, both of one tenant."""
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
        tenant_id=tenant,
        tenant_name="routes",
        scopes=["check"] if scopes is None else scopes,
        plan_id="basic",
        key_prefix="cs_routes",
    )
    app.dependency_overrides[get_current_student] = lambda: StudentContext(
        student_id=student, tenant_id=tenant, login="routes", display_name=None
    )


@pytest.fixture()
def routes(
    world: World, session_factory: async_sessionmaker[AsyncSession]
) -> Generator[AsyncMock]:
    """The app on the live database, a storage and a queue double; the owner calls."""
    s3 = AsyncMock()
    s3.upload_smart = AsyncMock(return_value=("s3://bucket/stored", 64))
    s3.delete_object = AsyncMock()
    arq = AsyncMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq:hw:1"))

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_s3_client] = lambda: s3
    app.dependency_overrides[get_arq_redis] = lambda: arq
    _act_as(tenant=world.tenant, student=world.student)
    yield s3
    app.dependency_overrides.clear()


async def _request(
    route: str,
    *,
    task: uuid.UUID,
    course: uuid.UUID,
    node: uuid.UUID | None = None,
    answers: dict[str, list[str]] | None = None,
    test_version: str | None = None,
) -> Response:
    """One call of a test route; ``course`` and ``node`` are the channel's to name."""
    given = _ANSWERS if answers is None else answers
    version = {} if test_version is None else {"test_version": test_version}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        if route == "portal-structure":
            return await client.get(f"/api/v1/portal/tasks/{task}/test")
        if route == "channel-structure":
            return await client.get(f"/api/v1/homework/tasks/{task}/test")
        if route == "portal-submit":
            return await client.post(
                f"/api/v1/portal/tasks/{task}/test-submissions",
                json={"answers": given, **version},
            )
        return await client.post(
            "/api/v1/homework/submit-test",
            json={
                "student_external_id": "ext-routes",
                "course_node_id": str(course),
                "node_id": str(node or course),
                "authored_document_id": str(task),
                "answers": given,
                **version,
            },
        )


async def _send_a_file(
    entry: str, *, task: uuid.UUID, course: uuid.UUID, node: uuid.UUID | None = None
) -> Response:
    """The same task through the entry's file route, for the bytes it refuses with."""
    file = {"file": ("answers.txt", b"1. b\n2. c\n3. a\n", "text/plain")}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        if entry == "channel":
            return await client.post(
                "/api/v1/homework/submit",
                data={
                    "student_external_id": "ext-routes",
                    "course_node_id": str(course),
                    "node_id": str(node or course),
                    "authored_document_id": str(task),
                },
                files=file,
            )
        return await client.post(f"/api/v1/portal/tasks/{task}/submissions", files=file)


async def _rows(
    session_factory: async_sessionmaker[AsyncSession], task: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(HomeworkSubmission)
                .where(HomeworkSubmission.authored_document_id == task)
            )
            or 0
        )


async def _unready(
    session_factory: async_sessionmaker[AsyncSession], task: uuid.UUID
) -> None:
    """The task's summary back to ``pending``: the task is not ready."""
    async with session_factory() as session:
        await session.execute(
            update(DocumentSummary)
            .where(DocumentSummary.authored_document_id == task)
            .values(status="pending")
        )
        await session.commit()


async def _drop_the_key(
    session_factory: async_sessionmaker[AsyncSession], task: uuid.UUID
) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(TaskReferenceOverride).where(
                TaskReferenceOverride.authored_document_id == task
            )
        )
        await session.commit()


def _every_name(value: object) -> set[str]:
    """Every key of every mapping in a JSON value, at any depth."""
    if isinstance(value, dict):
        return set(value).union(*(_every_name(item) for item in value.values()))
    if isinstance(value, list):
        return set[str]().union(*(_every_name(item) for item in value))
    return set()


class TestWhatTheStructureShows:
    @pytest.mark.parametrize("route", ["portal-structure", "channel-structure"])
    async def test_its_fields_listed_and_nothing_of_the_key(
        self, routes: AsyncMock, world: World, route: str
    ) -> None:
        """The field set, listed at every level; the version and the flag are there."""
        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            response = await _request(route, task=world.test, course=world.course)

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == _TOP
        for question in body["questions"]:
            assert set(question) == _QUESTION
            for option in question["options"]:
                assert set(option) == _OPTION
        assert not _every_name(body) & _OF_THE_KEY
        assert body["version"] == _HASH
        assert body["accepting_answers"] is True
        assert [q["number"] for q in body["questions"]] == ["1", "2", "3"]
        second = body["questions"][1]
        assert second["text"] == "Друге?"
        assert [(o["label"], o["text"]) for o in second["options"]] == [
            ("а", "так"),
            ("в", "ні"),
        ]

    @pytest.mark.parametrize("route", ["portal-structure", "channel-structure"])
    @pytest.mark.parametrize("case", ["tests-still-on-todays-mentor", "no-key"])
    async def test_it_says_when_answers_are_not_taken(
        self,
        routes: AsyncMock,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
        route: str,
        case: str,
    ) -> None:
        """Still shown — the flag is what tells the form it would be refused."""
        if case == "no-key":
            await _drop_the_key(session_factory, world.test)

        with patch(_SWITCH, return_value=_config(test_on_new_path=case == "no-key")):
            response = await _request(route, task=world.test, course=world.course)

        assert response.status_code == 200, response.text
        assert response.json()["accepting_answers"] is False


class TestAnotherTenantsTask:
    @pytest.mark.parametrize("route", _ROUTES)
    @pytest.mark.parametrize("ready", [True, False], ids=["ready", "not-ready"])
    async def test_is_a_missing_task_byte_for_byte(
        self,
        routes: AsyncMock,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
        route: str,
        ready: bool,
    ) -> None:
        """The stranger asks for the owner's test and for a task that is not there.

        On the channel's submission the stranger names its own course, as a
        channel would. A task that is not ready is still a missing one: a 409
        would tell the stranger it exists.
        """
        if not ready:
            await _unready(session_factory, world.test)
        _act_as(tenant=world.stranger, student=world.stranger_student)

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            foreign = await _request(
                route, task=world.test, course=world.stranger_course
            )
            missing = await _request(
                route, task=uuid.uuid4(), course=world.stranger_course
            )

        assert foreign.status_code == 404, foreign.text
        assert foreign.content == missing.content
        assert await _rows(session_factory, world.test) == 0

    async def test_the_owners_course_is_a_missing_course_to_a_channel(
        self, routes: AsyncMock, world: World
    ) -> None:
        """The channel's submission names the course too; the owner's is not there."""
        _act_as(tenant=world.stranger, student=world.stranger_student)

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            foreign = await _request(
                "channel-submit", task=world.test, course=world.course
            )
            missing = await _request(
                "channel-submit", task=uuid.uuid4(), course=uuid.uuid4()
            )

        assert foreign.status_code == 404, foreign.text
        assert foreign.content == missing.content

    async def test_an_enrollment_across_tenants_is_still_a_missing_task(
        self,
        routes: AsyncMock,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The portal's tenant filter on its own, with enrollment out of its way.

        The stranger's student is enrolled in the owner's course by a direct
        write: the bind route refuses it (``students.py`` checks the tenant of
        the student and of the course), and it is the one state in which the
        enrollment gate lets the stranger through and the filter alone refuses.
        """
        async with session_factory() as session:
            session.add(
                StudentEnrollment(
                    student_id=world.stranger_student, course_node_id=world.course
                )
            )
            await session.commit()
        _act_as(tenant=world.stranger, student=world.stranger_student)

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            foreign = await _request(
                "portal-structure", task=world.test, course=world.course
            )
            missing = await _request(
                "portal-structure", task=uuid.uuid4(), course=world.course
            )

        assert foreign.status_code == 404, foreign.text
        assert foreign.content == missing.content


class TestADeletedTask:
    @pytest.mark.parametrize("route", _ROUTES)
    async def test_is_a_missing_task_byte_for_byte(
        self,
        routes: AsyncMock,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
        route: str,
    ) -> None:
        """The owner's own test, soft-deleted: gone from every route alike."""
        async with session_factory() as session:
            await session.execute(
                update(AuthoredDocument)
                .where(AuthoredDocument.id == world.test)
                .values(deleted_at=func.now())
            )
            await session.commit()

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            deleted = await _request(route, task=world.test, course=world.course)
            missing = await _request(route, task=uuid.uuid4(), course=world.course)

        assert deleted.status_code == 404, deleted.text
        assert deleted.content == missing.content


class TestTheDoorsCodes:
    @pytest.mark.parametrize("route", ["portal-submit", "channel-submit"])
    @pytest.mark.parametrize(
        ("case", "status", "code"),
        [
            ("tests-still-on-todays-mentor", 409, "TEST_FORM_UNAVAILABLE"),
            ("not-a-test", 422, "NOT_A_TEST_TASK"),
            ("another-version", 409, "TEST_VERSION_CHANGED"),
            ("no-key", 409, "TEST_NOT_READY"),
            ("a-question-the-test-lacks", 422, "ANSWERS_DO_NOT_MATCH_TEST"),
        ],
    )
    async def test_reach_the_entry_as_code_and_details(
        self,
        routes: AsyncMock,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
        route: str,
        case: str,
        status: int,
        code: str,
    ) -> None:
        """Each with its status and body; nothing stored, no submission row."""
        task = world.plain if case == "not-a-test" else world.test
        answers = {"9": ["а"]} if case == "a-question-the-test-lacks" else None
        test_version = "e" * 64 if case == "another-version" else None
        if case == "no-key":
            await _drop_the_key(session_factory, world.test)
        switched_on = case != "tests-still-on-todays-mentor"
        before = await _rows(session_factory, task)

        with patch(_SWITCH, return_value=_config(test_on_new_path=switched_on)):
            response = await _request(
                route,
                task=task,
                course=world.course,
                answers=answers,
                test_version=test_version,
            )

        assert response.status_code == status, response.text
        body = response.json()
        assert set(body) == {"detail"}
        assert set(body["detail"]) == {"code", "details"}
        assert body["detail"]["code"] == code
        assert isinstance(body["detail"]["details"], str)
        assert body["detail"]["details"]
        assert await _rows(session_factory, task) == before
        routes.upload_smart.assert_not_awaited()

    @pytest.mark.parametrize("route", ["portal-structure", "channel-structure"])
    async def test_a_task_that_is_not_a_test_is_said_so_by_the_structure(
        self, routes: AsyncMock, world: World, route: str
    ) -> None:
        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            response = await _request(route, task=world.plain, course=world.course)

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert set(detail) == {"code", "details"}
        assert detail["code"] == "NOT_A_TEST_TASK"


class TestTheChannelsScope:
    @pytest.mark.parametrize("route", ["channel-structure", "channel-submit"])
    async def test_a_key_without_check_is_refused_as_on_the_file_route(
        self,
        routes: AsyncMock,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
        route: str,
    ) -> None:
        _act_as(tenant=world.tenant, student=world.student, scopes=["prep"])

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            refused = await _request(route, task=world.test, course=world.course)
            on_the_file_route = await _send_a_file(
                "channel", task=world.test, course=world.course
            )

        assert refused.status_code == 403, refused.text
        assert refused.json() == {"detail": "Requires scope: check"}
        assert refused.content == on_the_file_route.content
        assert await _rows(session_factory, world.test) == 0


class TestTheTreesTestForm:
    @pytest.mark.parametrize(
        "switched_on", [True, False], ids=["on-the-new-path", "on-todays-mentor"]
    )
    async def test_only_a_test_and_only_on_the_new_path(
        self, routes: AsyncMock, world: World, switched_on: bool
    ) -> None:
        """The flag the portal shows the form by (``DD-SP-BD``)."""
        with patch(_SWITCH, return_value=_config(test_on_new_path=switched_on)):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    f"/api/v1/portal/courses/{world.course}/materials"
                )

        assert response.status_code == 200, response.text
        flags = {item["id"]: item["test_form"] for item in response.json()["documents"]}
        assert flags == {
            str(world.test): switched_on,
            str(world.plain): False,
            str(world.material): False,
        }


class TestAnswersComeIn:
    @pytest.mark.parametrize(
        ("route", "mode"), [("portal-submit", "in_app"), ("channel-submit", "webhook")]
    )
    async def test_as_a_submission_of_their_entry(
        self,
        routes: AsyncMock,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
        route: str,
        mode: str,
    ) -> None:
        """202, a row delivered the entry's way, the canonical answers stored."""
        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            response = await _request(
                route, task=world.test, course=world.course, test_version=_HASH
            )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "received"
        assert body["duplicate"] is False
        async with session_factory() as session:
            row = await session.get(
                HomeworkSubmission, uuid.UUID(body["submission_id"])
            )
        assert row is not None
        assert row.authored_document_id == world.test
        assert row.delivery_mode == mode
        call = routes.upload_smart.await_args.kwargs
        stored = b"".join([chunk async for chunk in call["stream"]])
        assert stored == canonical_answers_json(_ANSWERS).encode("utf-8")


class TestTheGatesSharedWithAFile:
    """What the routes claim of the file routes, held by the bytes of both."""

    @pytest.mark.parametrize(
        ("route", "entry"),
        [
            ("portal-structure", "portal"),
            ("portal-submit", "portal"),
            ("channel-structure", "channel"),
            ("channel-submit", "channel"),
        ],
    )
    async def test_a_task_not_ready_is_refused_in_the_file_routes_words(
        self,
        routes: AsyncMock,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
        route: str,
        entry: str,
    ) -> None:
        await _unready(session_factory, world.test)

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            refused = await _request(route, task=world.test, course=world.course)
            on_the_file_route = await _send_a_file(
                entry, task=world.test, course=world.course
            )

        assert refused.status_code == 409, refused.text
        assert refused.content == on_the_file_route.content

    @pytest.mark.parametrize(
        "case",
        [
            "another-tenants-course",
            "a-course-that-is-not-a-root",
            "a-node-that-is-not-there",
            "a-task-that-is-not-there",
            "a-document-that-is-not-a-task",
        ],
    )
    async def test_the_channels_anchors_refuse_as_its_file_route(
        self, routes: AsyncMock, world: World, case: str
    ) -> None:
        course, node, task = world.course, world.course, world.test
        if case == "another-tenants-course":
            course = world.stranger_course
        elif case == "a-course-that-is-not-a-root":
            course = world.child
        elif case == "a-node-that-is-not-there":
            node = uuid.uuid4()
        elif case == "a-task-that-is-not-there":
            task = uuid.uuid4()
        else:
            task = world.material

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            refused = await _request(
                "channel-submit", task=task, course=course, node=node
            )
            on_the_file_route = await _send_a_file(
                "channel", task=task, course=course, node=node
            )

        assert refused.status_code in {404, 422}, refused.text
        assert refused.content == on_the_file_route.content

    @pytest.mark.parametrize("route", ["portal-structure", "portal-submit"])
    @pytest.mark.parametrize(
        "case",
        ["a-student-not-enrolled", "a-hidden-task", "a-document-that-is-not-a-task"],
    )
    async def test_the_portals_access_refuses_as_its_file_route(
        self, routes: AsyncMock, world: World, route: str, case: str
    ) -> None:
        task = world.test
        if case == "a-student-not-enrolled":
            _act_as(tenant=world.tenant, student=world.outsider)
        elif case == "a-hidden-task":
            task = world.hidden
        else:
            task = world.material

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            refused = await _request(route, task=task, course=world.course)
            on_the_file_route = await _send_a_file(
                "portal", task=task, course=world.course
            )

        assert refused.status_code == 404, refused.text
        assert refused.content == on_the_file_route.content
