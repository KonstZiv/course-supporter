"""A written test on the portal and in the author's tree, live database (task 07b).

What commit D2 of task 07b changes, one class per lock:

- the student's tree shows a test only once it is written in the system and
  published (decision 8; PRE-FLIGHT section 9, row 1): a draft is not there, a
  published test is, by its title and with the test form, and a test written
  as a file is not there either (decision 11) — whatever the switch;
- the portal's material route answers a written test, draft or published, as a
  missing material, byte for byte (decision 7): it is no file to render;
- the author's tree shows the draft by its title (decisions 7 and 8);
- the title is the label a task carries in the cost and the feedback
  breakdowns — ``material_label`` in its two other calls, through the
  repositories that read the column.

The switch and the storage are doubles; the database is real.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_current_student,
    get_current_tenant,
    get_s3_client,
)
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.feedback_kinds import (
    FeedbackKind,
    FeedbackTargetKind,
    FeedbackValue,
)
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    SubmissionState,
)
from course_supporter.homework.test_object import (
    DraftBody,
    DraftOption,
    DraftQuestion,
    published_form,
    version_digests,
)
from course_supporter.storage.content_hash import compute_content_hash
from course_supporter.storage.database import get_session
from course_supporter.storage.feedback_repository import FeedbackRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    StudentEnrollment,
    Tenant,
)
from course_supporter.storage.test_object_repository import TestObjectRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_TITLE = "Тест до лекції 3"
_SWITCH = "course_supporter.homework.test_doors.get_path_config"
_DRAFT = DraftBody(
    questions=(
        DraftQuestion(
            text="Перше?",
            options=(
                DraftOption(text="так", correct=True),
                DraftOption(text="ні", correct=False),
            ),
        ),
    )
)
_CALLED_AT = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
"""When the review's model call was made — inside the cost breakdown's period."""


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
    """One course: a written test (a draft), a test written as a file, a material."""

    tenant: uuid.UUID
    course: uuid.UUID
    written: uuid.UUID
    text_test: uuid.UUID
    material: uuid.UUID
    student: uuid.UUID


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[World]:
    """The written test has a draft and no version; the student is enrolled."""
    async with session_factory() as session:
        tenant = Tenant(name=f"portal-07b-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        course = make_root_course_node(tenant_id=tenant.id, title="Курс", order=0)
        session.add(course)
        await session.flush()
        material = AuthoredDocument(
            course_node_id=course.id,
            course_root_id=course.id,
            source_type="web",
            source_url="https://example.test/lecture",
            order=0,
            language="ukr",
        )
        written = AuthoredDocument(
            course_node_id=course.id,
            course_root_id=course.id,
            source_type="test_object",
            source_url="test-object:",
            task_type="test",
            order=1,
            language="ukr",
            content_hash=compute_content_hash(b"", []),
            title=_TITLE,
        )
        text_test = AuthoredDocument(
            course_node_id=course.id,
            course_root_id=course.id,
            source_type="text",
            source_url="https://example.test/test.md",
            filename="test.md",
            task_type="test",
            order=2,
            language="ukr",
        )
        session.add_all([material, written, text_test])
        await session.flush()
        await TestObjectRepository(session).replace_draft(written.id, _DRAFT.to_jsonb())
        student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:6]}")
        session.add(student)
        await session.flush()
        session.add(StudentEnrollment(student_id=student.id, course_node_id=course.id))
        await session.commit()
        built = World(
            tenant=tenant.id,
            course=course.id,
            written=written.id,
            text_test=text_test.id,
            material=material.id,
            student=student.id,
        )

    yield built

    async with session_factory() as session:
        jobs = select(Job.id).where(Job.tenant_id == built.tenant)
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id.in_(jobs))
        )
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == built.tenant
            )
        )
        await session.execute(delete(Job).where(Job.tenant_id == built.tenant))
        await session.execute(
            delete(StudentEnrollment).where(
                StudentEnrollment.student_id == built.student
            )
        )
        await session.execute(delete(Student).where(Student.tenant_id == built.tenant))
        await session.execute(delete(Tenant).where(Tenant.id == built.tenant))
        await session.commit()


@pytest.fixture()
def routes(
    world: World, session_factory: async_sessionmaker[AsyncSession]
) -> Generator[None]:
    """The app on the live database: the enrolled student, the author's key."""
    storage = MagicMock()
    storage.extract_key = MagicMock(return_value=None)

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_s3_client] = lambda: storage
    app.dependency_overrides[get_current_student] = lambda: StudentContext(
        student_id=world.student,
        tenant_id=world.tenant,
        login="portal",
        display_name=None,
    )
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
        tenant_id=world.tenant,
        tenant_name="portal",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_portal",
    )
    yield
    app.dependency_overrides.clear()


async def _get(path: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


def _shown(node: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every document of a portal tree, at any depth, by its id."""
    found = {item["id"]: item for item in node["documents"]}
    for child in node["children"]:
        found |= _shown(child)
    return found


async def _publish(
    session_factory: async_sessionmaker[AsyncSession], document: uuid.UUID
) -> None:
    """A publication as the service makes it, without asking for explanations."""
    body = published_form(_DRAFT, "ukr")
    digests = version_digests(body, "ukr")
    async with session_factory() as session:
        await TestObjectRepository(session).publish(
            document,
            language="ukr",
            body=body.to_jsonb(),
            content_digest=digests.content_digest,
            answers_digest=digests.answers_digest,
            publication_digest=digests.publication_digest,
        )
        await session.commit()


class TestTheStudentsTree:
    """A test is shown once it is written in the system and published."""

    @pytest.mark.parametrize(
        "switched_on", [True, False], ids=["on-the-new-path", "on-todays-mentor"]
    )
    async def test_a_draft_is_not_there(
        self, routes: None, world: World, switched_on: bool
    ) -> None:
        """Before its first publication (decision 8), whatever the switch."""
        with patch(_SWITCH, return_value=_config(test_on_new_path=switched_on)):
            tree = await _get(f"/api/v1/portal/courses/{world.course}/materials")

        assert tree.status_code == 200, tree.text
        shown = _shown(tree.json())
        assert str(world.material) in shown, "the premise: the tree is read"
        assert str(world.written) not in shown

    async def test_once_published_it_is_there_by_its_title_with_the_test_form(
        self,
        routes: None,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Labelled by its title (decision 7), answered with the test form."""
        await _publish(session_factory, world.written)

        with patch(_SWITCH, return_value=_config(test_on_new_path=True)):
            tree = await _get(f"/api/v1/portal/courses/{world.course}/materials")

        assert tree.status_code == 200, tree.text
        item = _shown(tree.json())[str(world.written)]
        assert (item["label"], item["kind"], item["task_type"], item["test_form"]) == (
            _TITLE,
            "task",
            "test",
            True,
        )

    @pytest.mark.parametrize(
        "switched_on", [True, False], ids=["on-the-new-path", "on-todays-mentor"]
    )
    async def test_a_test_written_as_a_file_is_not_there(
        self, routes: None, world: World, switched_on: bool
    ) -> None:
        """No longer answered (decision 11), so not shown, whatever the switch."""
        with patch(_SWITCH, return_value=_config(test_on_new_path=switched_on)):
            tree = await _get(f"/api/v1/portal/courses/{world.course}/materials")

        assert tree.status_code == 200, tree.text
        shown = _shown(tree.json())
        assert str(world.material) in shown, "the premise: the tree is read"
        assert str(world.text_test) not in shown


class TestThePortalsMaterialRoute:
    """A written test is no material to render (decision 7)."""

    @pytest.mark.parametrize("published", [False, True], ids=["draft", "published"])
    async def test_answers_a_written_test_as_a_missing_material(
        self,
        routes: None,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
        published: bool,
    ) -> None:
        """Byte for byte — not the placeholder as an external link."""
        if published:
            await _publish(session_factory, world.written)

        served = await _get(f"/api/v1/portal/materials/{world.material}")
        written = await _get(f"/api/v1/portal/materials/{world.written}")
        missing = await _get(f"/api/v1/portal/materials/{uuid.uuid4()}")

        assert served.status_code == 200, "the premise: the route serves a material"
        assert missing.status_code == 404, missing.text
        assert (written.status_code, written.content) == (404, missing.content)


class TestTheAuthorsTree:
    async def test_shows_the_draft_by_its_title(
        self,
        routes: None,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The author sees the draft (decision 8) by the name both trees show."""
        async with session_factory() as session:
            version = await TestObjectRepository(session).latest_version(world.written)
        assert version is None, "the premise: nothing is published"

        detail = await _get(f"/api/v1/nodes/{world.course}/detail")

        assert detail.status_code == 200, detail.text
        items = {item["id"]: item for item in detail.json()["authored_documents"]}
        assert items[str(world.written)]["title"] == _TITLE
        assert items[str(world.material)]["title"] is None


async def _a_reviewed_and_paid_attempt(
    session_factory: async_sessionmaker[AsyncSession], world: World, task: uuid.UUID
) -> None:
    """A reviewed submission to ``task``: its job's paid call and a student's touch."""
    async with session_factory() as session:
        job = Job(
            tenant_id=world.tenant,
            course_node_id=world.course,
            job_type="homework_processing",
            status="complete",
        )
        session.add(job)
        await session.flush()
        submission = HomeworkSubmission(
            tenant_id=world.tenant,
            student_id=world.student,
            course_node_id=world.course,
            node_id=world.course,
            authored_document_id=task,
            file_url="s3://bucket/homework/answers.json",
            file_type="application/json",
            original_filename="answers.json",
            delivery_mode="in_app",
            status="completed",
            review_markdown="# Review",
            job_id=job.id,
        )
        session.add(submission)
        await session.flush()
        session.add(
            ExternalServiceCall(
                job_id=job.id,
                provider="deepseek",
                model_id="deepseek-v4",
                cost_usd=0.25,
                action="review",
                created_at=_CALLED_AT,
            )
        )
        await FeedbackRepository(session).record(
            tenant_id=world.tenant,
            student_id=world.student,
            target_kind=FeedbackTargetKind.REVIEW,
            target_id=submission.id,
            kind=FeedbackKind.TOUCH,
            value=FeedbackValue.HELPED,
        )
        await session.commit()


class TestTheTitleIsTheTasksLabel:
    """``material_label``'s other two calls, and the repositories they read."""

    async def test_in_the_cost_breakdown(
        self,
        routes: None,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A task with a title is labelled by it; one without keeps its filename."""
        await _a_reviewed_and_paid_attempt(session_factory, world, world.written)
        await _a_reviewed_and_paid_attempt(session_factory, world, world.text_test)

        response = await _get(
            f"/api/v1/cost/homework/course/{world.course}?from=2026-01-01&to=2026-12-31"
        )

        assert response.status_code == 200, response.text
        labels = {
            row["authored_document_id"]: row["task_label"]
            for row in response.json()["by_task"]
        }
        assert labels == {str(world.written): _TITLE, str(world.text_test): "test.md"}

    async def test_in_the_feedback_breakdown(
        self,
        routes: None,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The same label the cost pages show, from the same helper."""
        await _a_reviewed_and_paid_attempt(session_factory, world, world.written)
        await _a_reviewed_and_paid_attempt(session_factory, world, world.text_test)

        response = await _get(f"/api/v1/feedback/counters/course/{world.course}")

        assert response.status_code == 200, response.text
        labels = {
            row["authored_document_id"]: row["task_label"]
            for row in response.json()["by_task"]
        }
        assert labels == {str(world.written): _TITLE, str(world.text_test): "test.md"}
