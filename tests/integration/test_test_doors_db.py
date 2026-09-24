"""The doors of a test, and the core that stores its answers (task 07).

Two halves. The file routes: a file sent to a test is refused — through both
routes, over HTTP, with a live database — once tests are on the new path, and
taken exactly as before while they are not. The core: a test's answers go
through the test's doors and are stored as a file is, with no deduplication.

What every refusal is measured by: the submission rows of the task before and
after, and the storage double's upload — a refusal writes no row and stores
nothing (task 07, decision 12). The storage and the queue are the doubles; the
database is real.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
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
from course_supporter.homework.submission_core import (
    create_and_dispatch_test_submission,
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
_HASH = "d1" + "0" * 62
_KEY: dict[str, list[str]] = {"1": ["б"], "2": ["в"], "3": ["а"]}
_ANSWERS: dict[str, list[str]] = {"1": ["б"], "2": ["а"], "3": ["а"]}
_SWITCH = "course_supporter.homework.test_doors.get_path_config"


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


async def _task(
    session: AsyncSession, node: CourseNode, *, task_type: str = "test"
) -> AuthoredDocument:
    """A ready task with the test's text; a key is added separately."""
    document = AuthoredDocument(
        course_node_id=node.id,
        course_root_id=node.id,
        source_type="text",
        source_url="https://example.test/task",
        task_type=task_type,
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


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """A tenant, a course, a test with its key, a plain task, an enrolled student."""
    async with session_factory() as session:
        tenant = Tenant(name=f"doors-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="Doors", order=0)
        session.add(node)
        await session.flush()
        test = await _task(session, node)
        plain = await _task(session, node, task_type="task")
        await TaskReferenceRepository(session).replace_override(
            authored_document_id=test.id,
            kind=ReferenceKind.TEST_KEY,
            answers=_KEY,
            source_content_hash=_HASH,
        )
        student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:6]}")
        session.add(student)
        await session.flush()
        session.add(StudentEnrollment(student_id=student.id, course_node_id=node.id))
        await session.commit()
        ids = {
            "tenant_id": tenant.id,
            "node_id": node.id,
            "test_id": test.id,
            "plain_id": plain.id,
            "student_id": student.id,
        }

    yield ids

    async with session_factory() as session:
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == ids["tenant_id"]
            )
        )
        await session.execute(delete(Job).where(Job.tenant_id == ids["tenant_id"]))
        await session.execute(
            delete(StudentEnrollment).where(
                StudentEnrollment.student_id == ids["student_id"]
            )
        )
        await session.execute(
            delete(Student).where(Student.tenant_id == ids["tenant_id"])
        )
        await session.execute(delete(Tenant).where(Tenant.id == ids["tenant_id"]))
        await session.commit()


def _storage() -> AsyncMock:
    s3 = AsyncMock()
    s3.upload_smart = AsyncMock(return_value=("s3://bucket/stored", 64))
    s3.delete_object = AsyncMock()
    return s3


def _queue() -> AsyncMock:
    arq = AsyncMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq:hw:1"))
    return arq


async def _rows(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(HomeworkSubmission)
                .where(HomeworkSubmission.authored_document_id == task_id)
            )
            or 0
        )


# ── The file routes ─────────────────────────────────────────────────────


@pytest.fixture()
def routes(
    world: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> Generator[AsyncMock]:
    """Both file routes wired to the live database, a storage and a queue double."""
    s3 = _storage()
    arq = _queue()

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
        tenant_id=world["tenant_id"],
        tenant_name="doors",
        scopes=["check"],
        plan_id="basic",
        key_prefix="cs_doors",
    )
    app.dependency_overrides[get_current_student] = lambda: StudentContext(
        student_id=world["student_id"],
        tenant_id=world["tenant_id"],
        login="doors",
        display_name=None,
    )
    app.dependency_overrides[get_s3_client] = lambda: s3
    app.dependency_overrides[get_arq_redis] = lambda: arq
    yield s3
    app.dependency_overrides.clear()


async def _send_a_file(route: str, world: dict[str, uuid.UUID]) -> Any:
    """One text file to the test, through the named route."""
    file = {"file": ("answers.txt", b"1. b\n2. c\n3. a\n", "text/plain")}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        if route == "homework":
            return await client.post(
                "/api/v1/homework/submit",
                data={
                    "student_external_id": "ext-doors",
                    "course_node_id": str(world["node_id"]),
                    "node_id": str(world["node_id"]),
                    "authored_document_id": str(world["test_id"]),
                },
                files=file,
            )
        return await client.post(
            f"/api/v1/portal/tasks/{world['test_id']}/submissions", files=file
        )


class TestAFileSentToATest:
    @pytest.mark.parametrize("route", ["homework", "portal"])
    async def test_is_refused_once_tests_are_on_the_new_path(
        self,
        routes: AsyncMock,
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        route: str,
    ) -> None:
        """422 with its code, before the upload: no row, nothing stored."""
        before = await _rows(session_factory, world["test_id"])

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            response = await _send_a_file(route, world)

        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "TEST_ANSWERS_REQUIRED"
        assert await _rows(session_factory, world["test_id"]) == before
        routes.upload_smart.assert_not_awaited()

    @pytest.mark.parametrize("route", ["homework", "portal"])
    async def test_is_taken_as_before_while_tests_are_on_todays_mentor(
        self,
        routes: AsyncMock,
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        route: str,
    ) -> None:
        """The way back: with the switch off, a file is still a test's form."""
        before = await _rows(session_factory, world["test_id"])

        with patch(_SWITCH, return_value=_config(test_on_new_path=False)):
            response = await _send_a_file(route, world)

        assert response.status_code == 202, response.text
        assert await _rows(session_factory, world["test_id"]) == before + 1
        routes.upload_smart.assert_awaited_once()


# ── The core ────────────────────────────────────────────────────────────


async def _submit(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, uuid.UUID],
    *,
    answers: dict[str, list[str]],
    task_id: uuid.UUID | None = None,
    test_version: str | None = None,
    s3: AsyncMock | None = None,
    resolve_student: AsyncMock | None = None,
) -> Any:
    """One call of the core, as a route would make it."""
    async with session_factory() as session:
        task = await session.get(AuthoredDocument, task_id or world["test_id"])
        assert task is not None
        student = await session.get(Student, world["student_id"])
        assert student is not None
        return await create_and_dispatch_test_submission(
            session=session,
            s3=s3 or _storage(),
            arq=_queue(),
            tenant_id=world["tenant_id"],
            resolve_student=resolve_student or AsyncMock(return_value=(student, False)),
            course_node_id=world["node_id"],
            node_id=world["node_id"],
            task_doc=task,
            answers=answers,
            test_version=test_version,
            delivery_mode="in_app",
        )


class TestTheAnswersOfATest:
    async def test_the_same_answers_twice_are_two_submissions(
        self,
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No deduplication: every attempt at a test is scored anew (decision 7).

        The first attempt is REVIEWED before the second arrives — the only state
        a file's deduplication looks for (``find_duplicate``: completed or
        delivered). With the first still waiting, a test that turned
        deduplication on would pass all the same, and prove nothing.
        """
        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            first = await _submit(session_factory, world, answers=_ANSWERS)
            async with session_factory() as session:
                reviewed = await session.get(HomeworkSubmission, first.submission.id)
                assert reviewed is not None
                reviewed.status = "delivered"
                await session.commit()
            second = await _submit(session_factory, world, answers=dict(_ANSWERS))

        assert (first.duplicate, second.duplicate) == (False, False)
        assert first.submission.id != second.submission.id
        assert first.job_id != second.job_id
        assert first.submission.file_hash == second.submission.file_hash
        assert await _rows(session_factory, world["test_id"]) == 2

    async def test_what_is_stored_is_the_canonical_answers(
        self,
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Stored as a file is — and exactly the bytes the builder will read."""
        s3 = _storage()
        typed = {" 2 ": ["а)"], "1": ["Б"], "3": ["A"]}

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            result = await _submit(session_factory, world, answers=typed, s3=s3)

        call = s3.upload_smart.await_args.kwargs
        stored = b"".join([chunk async for chunk in call["stream"]])
        assert stored == canonical_answers_json(typed).encode("utf-8")
        assert stored == '{"1":["б"],"2":["а"],"3":["а"]}'.encode()
        assert call["content_type"] == "application/json"
        assert call["key"].endswith("/answers.json")
        submission = result.submission
        assert submission.file_hash == hashlib.sha256(stored).hexdigest()
        assert submission.file_type == "application/json"
        assert submission.original_filename == "answers.json"

    async def test_an_unanswered_question_is_taken(
        self,
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Counted wrong later, never refused here (decision 23)."""
        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            result = await _submit(session_factory, world, answers={"1": ["б"]})

        assert result.duplicate is False
        assert await _rows(session_factory, world["test_id"]) == 1


class TestEveryRefusalAtTheDoors:
    """Each refusal writes no submission row, stores nothing, resolves no student."""

    @pytest.mark.parametrize(
        ("case", "status", "code"),
        [
            ("another-version", 409, "TEST_VERSION_CHANGED"),
            ("no-key", 409, "TEST_NOT_READY"),
            ("a-question-the-test-lacks", 422, "ANSWERS_DO_NOT_MATCH_TEST"),
            ("an-option-the-question-lacks", 422, "ANSWERS_DO_NOT_MATCH_TEST"),
            ("tests-still-on-todays-mentor", 409, "TEST_FORM_UNAVAILABLE"),
            ("not-a-test", 422, "NOT_A_TEST_TASK"),
        ],
    )
    async def test_it_leaves_no_trace(
        self,
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        case: str,
        status: int,
        code: str,
    ) -> None:
        answers = dict(_ANSWERS)
        test_version: str | None = None
        task_id = world["test_id"]
        switched_on = True
        if case == "another-version":
            test_version = "e" * 64
        elif case == "no-key":
            async with session_factory() as session:
                await session.execute(
                    delete(TaskReferenceOverride).where(
                        TaskReferenceOverride.authored_document_id == task_id
                    )
                )
                await session.commit()
        elif case == "a-question-the-test-lacks":
            answers = {"9": ["а"]}
        elif case == "an-option-the-question-lacks":
            answers = {"1": ["г"]}
        elif case == "tests-still-on-todays-mentor":
            switched_on = False
        else:
            task_id = world["plain_id"]
        s3 = _storage()
        student = AsyncMock()
        before = await _rows(session_factory, task_id)

        with (
            patch(_SWITCH, return_value=_config(test_on_new_path=switched_on)),
            pytest.raises(HTTPException) as refused,
        ):
            await _submit(
                session_factory,
                world,
                answers=answers,
                task_id=task_id,
                test_version=test_version,
                s3=s3,
                resolve_student=student,
            )

        assert refused.value.status_code == status
        assert refused.value.detail["code"] == code
        assert await _rows(session_factory, task_id) == before
        s3.upload_smart.assert_not_awaited()
        student.assert_not_awaited()
