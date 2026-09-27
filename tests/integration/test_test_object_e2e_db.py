"""Acceptance of task 07b, walked end to end: a test written in the system.

The criterion this file exists for is a THROUGH one (acceptance 2 of task 07b)
— a test created from YAML is a draft; published, it asks for exactly one
generation of its explanations; its structure shows nothing of the key at any
depth; an answer is reviewed; a question added and published again is a new
version and exactly one new generation; the old attempt's review stands; a new
attempt is graded by the new version; an identical draft published again costs
no version and no job; and creating, editing, uploading and submitting call no
model. A through criterion is not proved by the tests that hold each joint,
each of which can be green while the joints between them are broken
(``vision-rules#25``).

So the walk goes only through the routes a client uses in production. The
author uploads the test's YAML file to the document route, edits and publishes
it through the author's routes, with a key of scope PREP. The student reads it
in the tree and answers it through the portal, with a real bearer session from
a real login, as the portal's test form (P2) does — and its structure is held
by the very field lock the route tests hold for that form. The work the routes
queue is run by its real ARQ bodies, as the worker would. The database is live;
the storage, the queue, the webhook and the model are doubles, and the model's
double writes the register row a real call would have written.

Three things the register alone could not show:

- A call made while a request is served, outside any job, leaves NO register
  row: the register drops it and counts the drop (``DD-SP-AN``). So besides the
  rows, per job and per action, every step that serves a request or runs a job
  asserts that no row was dropped — a model called in a request would show
  there and nowhere else.
- The queue's double records what was put on it and on which queue, from the
  routes and from the worker alike. Step 11 reads every such call against the
  queue map of hot fix 5's lock (``tests/unit/test_queue_completeness.py``): a
  job goes to a queue whose worker runs it, and the explanations to arq's
  default one — the two a publication asks for, and the one a review asks for
  from the worker after delivery, in the student's language: the path of
  defect 1 of task 07. That is why the student answers step 8 in English, as
  the form lets them. Task 07's walk ran its jobs straight from their rows,
  and so could not see the queue its explanations went to.
- A version published while an attempt waits does not re-grade it (decision
  14). So the attempt of step 8 is answered under version 2 and waits in the
  queue while step 9 publishes a change of the pass mark alone; only then does
  its work run, and its verdict is version 2's.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from arq.constants import default_queue_name
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Text, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.agents.key_explainer import STAGE_NAME
from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.api.tasks import arq_process_homework
from course_supporter.auth.context import TenantContext
from course_supporter.funds_port import FUNDS_PORT_ACTION
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    SubmissionState,
)
from course_supporter.homework.test_doors import MISSING_TASK, read_stored_answers
from course_supporter.jobs import JobType
from course_supporter.service_logging import get_current_job_id, skipped_write_counts
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    StudentCredential,
    StudentCredentialToken,
    StudentEnrollment,
    TaskReference,
    Tenant,
    TestVersion,
)
from course_supporter.worker import HomeworkWorkerSettings
from course_supporter.workers.key_explain import arq_explain_key
from tests._helpers.course_node_factory import make_root_course_node
from tests.integration.test_test_routes_db import (
    _OF_THE_KEY,
    _OPTION,
    _QUESTION,
    _TOP,
    _every_name,
)
from tests.unit.test_queue_completeness import _served

pytestmark = pytest.mark.requires_db

_TITLE = "Тест до лекції 3 — робота з агентом"
_AUTHOR_ON_3 = "Тести одразу показують, чи не зламала зміна агента те, що працювало."
_YAML = (
    f"title: {_TITLE}\n"
    "pass_threshold: 80\n"
    "questions:\n"
    "  - text: Що агент робить першим, отримавши завдання?\n"
    "    options:\n"
    "      - text: Одразу пише код\n"
    "        correct: false\n"
    "      - text: Читає наявний код і тести\n"
    "        correct: true\n"
    "      - text: Видаляє старі файли\n"
    "        correct: false\n"
    "  - text: Як перевірити бібліотеку, яку запропонував агент?\n"
    "    options:\n"
    "      - text: Повірити агентові на слово\n"
    "        correct: false\n"
    "      - text: Попросити агента повторити назву\n"
    "        correct: false\n"
    "      - text: Знайти її в реєстрі пакетів\n"
    "        correct: true\n"
    "  - text: Навіщо тести в проєкті, де працює агент?\n"
    "    options:\n"
    "      - text: Вони показують, чи не зламала зміна те, що працювало\n"
    "        correct: true\n"
    "      - text: Щоб агентові було що читати\n"
    "        correct: false\n"
    "      - text: Вони там не потрібні\n"
    "        correct: false\n"
    f"    explanation: {_AUTHOR_ON_3}\n"
    "  - text: Що робити з великим завданням для агента?\n"
    "    options:\n"
    "      - text: Дати його цілим\n"
    "        correct: false\n"
    "      - text: Розбити на малі кроки\n"
    "        correct: true\n"
    "      - text: Відкласти\n"
    "        correct: false\n"
    "  - text: Який запит до агента найкращий?\n"
    "    options:\n"
    "      - text: «Виправ помилки»\n"
    "        correct: false\n"
    "      - text: «Зроби краще»\n"
    "        correct: false\n"
    "      - text: «Додай у parse_date перевірку порожнього рядка і тест на неї»\n"
    "        correct: true\n"
).encode()
"""The author's file: five questions, the key б, в, а, б, в, and a pass mark of 80."""
_KEY: dict[str, list[str]] = {
    "1": ["б"],
    "2": ["в"],
    "3": ["а"],
    "4": ["б"],
    "5": ["в"],
}
_WRONG_ON_5 = {**_KEY, "5": ["а"]}
_RIGHT_ON_5 = "в) «Додай у parse_date перевірку порожнього рядка і тест на неї»"
_ADDED: dict[str, Any] = {
    "text": "Чи варто перевіряти зміни агента тестами?",
    "options": [
        {"text": "Так", "correct": True},
        {"text": "Ні", "correct": False},
    ],
}
"""The question step 6 adds; its right option is а."""
_NEW_ATTEMPT = {**_WRONG_ON_5, "6": ["а"]}
"""Five of six right: 83 %, over version 2's pass mark of 80, under version 3's."""
_STRICTER = 90
_MODEL_1 = {number: f"Пояснення моделі до питання {number}." for number in "12345"}
_MODEL_2 = {
    number: f"Пояснення моделі до питання {number} у другій версії."
    for number in "123456"
}
_LOGIN = "e2e-07b-student"
_PASSWORD = "correct horse 07b"
_EXTERNAL_ID = "channel-e2e-07b-student"
_SWITCHES = (
    "course_supporter.homework.test_doors.get_path_config",
    "course_supporter.homework.path_runner.get_path_config",
)


def _config() -> PathConfig:
    """The shipped shape, with ``test`` on the new path and the rest on today's."""
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
                    "served_by": ServedBy.NEW_PATH.value,
                    "paths": {s.value: [] for s in SubmissionState},
                },
                "short_task": {"served_by": "todays_mentor", "paths": {}},
                "task": {"served_by": "todays_mentor", "paths": {}},
                "project": {"served_by": "todays_mentor", "paths": {}},
            },
        }
    )


class _ExplanationsModel:
    """The explanations' stage: canned answers, and the register row a call leaves."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.explanations: dict[str, str] = {}

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Any = None,
        expects_json: bool = False,
        **render_context: Any,
    ) -> Any:
        del expects_json, render_context
        from course_supporter.service_logging import _persist

        await _persist(
            self._session_factory,
            action=stage_name,
            strategy="default",
            provider="deepseek_thinking",
            model_id="deepseek-v4-pro",
            success=True,
            cost_usd=0.0184,
            unit_type="tokens",
            unit_in=900,
            unit_out=300,
        )
        if response_validator is not None:
            response_validator(
                json.dumps(
                    {
                        "explanations": self.explanations,
                        "doubts": dict.fromkeys(self.explanations, False),
                    }
                )
            )
        return None


class _NoModel:
    """A submission's stage router: a test's submission must never reach one."""

    async def execute_for_stage(self, stage_name: str, **_: Any) -> Any:
        msg = f"a submission of a test called a model: {stage_name}"
        raise AssertionError(msg)


class _Storage:
    """An S3 that keeps what it is given: the core's upload is the body's download."""

    _PREFIX = "http://s3.test/bucket/"

    def __init__(self, directory: Path) -> None:
        self.objects: dict[str, bytes] = {}
        self._directory = directory

    async def upload_smart(
        self,
        stream: AsyncIterator[bytes],
        key: str,
        content_type: str,
        *,
        file_size: int | None = None,
    ) -> tuple[str, int]:
        del content_type, file_size
        data = b"".join([chunk async for chunk in stream])
        self.objects[key] = data
        return self._PREFIX + key, len(data)

    def extract_key(self, url: str) -> str | None:
        return url.removeprefix(self._PREFIX) if url.startswith(self._PREFIX) else None

    async def download_file(self, key: str, dest: Path | None = None) -> Path:
        path = dest or self._directory / key.replace("/", "_")
        path.write_bytes(self.objects[key])
        return path

    async def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


def _key(tenant_id: uuid.UUID, *scopes: str) -> TenantContext:
    """A key context carrying exactly the scopes named — no more."""
    return TenantContext(
        tenant_id=tenant_id,
        tenant_name="e2e-07b",
        scopes=list(scopes),
        plan_id="basic",
        key_prefix="cs_e2e07b",
    )


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """A course in Ukrainian with a section, and a student enrolled in it.

    No test: creating it is the walk's first step, by the upload a client makes.
    """
    async with session_factory() as session:
        tenant = Tenant(
            name=f"e2e-07b-{uuid.uuid4().hex[:8]}",
            webhook_url="https://channel.example/hook",
        )
        session.add(tenant)
        await session.flush()
        course = make_root_course_node(tenant_id=tenant.id, title="E2E 07b", order=0)
        session.add(course)
        await session.flush()
        section = CourseNode(
            tenant_id=tenant.id, parent_id=course.id, title="Лекція 3", order=0
        )
        session.add(section)
        await session.flush()
        student = Student(tenant_id=tenant.id, external_id=_EXTERNAL_ID)
        session.add(student)
        await session.flush()
        session.add(StudentEnrollment(student_id=student.id, course_node_id=course.id))
        await session.commit()
        ids = {
            "tenant_id": tenant.id,
            "course_id": course.id,
            "section_id": section.id,
            "student_id": student.id,
        }

    yield ids

    async with session_factory() as session:
        jobs = select(Job.id).where(Job.tenant_id == ids["tenant_id"])
        students = select(Student.id).where(Student.tenant_id == ids["tenant_id"])
        credentials = select(StudentCredential.id).where(
            StudentCredential.student_id.in_(students)
        )
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id.in_(jobs))
        )
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == ids["tenant_id"]
            )
        )
        await session.execute(delete(Job).where(Job.tenant_id == ids["tenant_id"]))
        await session.execute(
            delete(StudentCredentialToken).where(
                StudentCredentialToken.credential_id.in_(credentials)
            )
        )
        await session.execute(
            delete(StudentCredential).where(StudentCredential.student_id.in_(students))
        )
        await session.execute(
            delete(StudentEnrollment).where(StudentEnrollment.student_id.in_(students))
        )
        await session.execute(
            delete(Student).where(Student.tenant_id == ids["tenant_id"])
        )
        await session.execute(delete(Tenant).where(Tenant.id == ids["tenant_id"]))
        await session.commit()


@dataclass(frozen=True)
class Client:
    """The live client, the switch for the next request's key, and the doubles."""

    ac: AsyncClient
    use_key: Callable[[TenantContext], None]
    storage: _Storage
    queue: AsyncMock


@pytest.fixture()
async def client(
    world: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> AsyncGenerator[Client]:
    """A live client over the real routes, with one queue for all the walk.

    ``get_current_student`` is NOT overridden: the portal half runs the real
    bearer flow. The queue is ONE double for the routes and for the worker
    both — it dispatches nothing, and it keeps every call, queue named and all.
    The ``Job`` rows are the real ones, and the test runs the work itself.
    """
    storage = _Storage(tmp_path)
    queue = AsyncMock(enqueue_job=AsyncMock(return_value=None))

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_arq_redis] = lambda: queue
    app.dependency_overrides[get_s3_client] = lambda: storage

    def use_key(ctx: TenantContext) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: ctx

    use_key(_key(world["tenant_id"], "prep"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield Client(ac=ac, use_key=use_key, storage=storage, queue=queue)
    app.dependency_overrides.clear()


async def _student_session(
    client: Client, world: dict[str, uuid.UUID]
) -> dict[str, str]:
    """The student's bearer header, from a login the tenant provisioned."""
    client.use_key(_key(world["tenant_id"], "prep"))
    provision = await client.ac.post(
        "/api/v1/students",
        json={
            "mode": "existing",
            "student_id": str(world["student_id"]),
            "login": _LOGIN,
            "password": _PASSWORD,
        },
    )
    assert provision.status_code == 201, provision.text
    login = await client.ac.post(
        "/api/v1/portal/login",
        json={
            "tenant_id": str(world["tenant_id"]),
            "login": _LOGIN,
            "password": _PASSWORD,
        },
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _written_back(draft: dict[str, Any]) -> dict[str, Any]:
    """The draft the author read, as the body the draft route takes back.

    The view adds what a publication would give — numbers and letters — and the
    body carries neither; a pass mark or an explanation not set is left out.
    """
    questions = []
    for question in draft["questions"]:
        written: dict[str, Any] = {
            "text": question["text"],
            "options": [
                {"text": option["text"], "correct": option["correct"]}
                for option in question["options"]
            ],
        }
        if question["explanation"] is not None:
            written["explanation"] = question["explanation"]
        questions.append(written)
    body: dict[str, Any] = {"questions": questions}
    if draft["pass_threshold"] is not None:
        body["pass_threshold"] = draft["pass_threshold"]
    return body


def _as_the_form_sends(
    sheet: dict[str, Any], ticked: dict[str, list[str]], language: str = "ukr"
) -> dict[str, Any]:
    """The body the portal's test form sends (``PortalTestForm.tsx``).

    Every question of the structure goes, a blank one as an empty list, with the
    structure's ``version`` and the review's language — the one the student
    picks, or else the course's.
    """
    return {
        "answers": {
            q["number"]: ticked.get(q["number"], []) for q in sheet["questions"]
        },
        "test_version": sheet["version"],
        "response_language": language,
    }


def _the_form_reads(sheet: dict[str, Any], step: int) -> None:
    """The structure field by field, as the form reads it: nothing of the key.

    The field lock of the route tests (``test_test_routes_db.py``) — the same
    sets, imported, not copied, so the two cannot drift apart.
    """
    assert set(sheet) == _TOP, f"step {step}: the three fields"
    for question in sheet["questions"]:
        assert set(question) == _QUESTION, f"step {step}: a question's fields"
        for option in question["options"]:
            assert set(option) == _OPTION, f"step {step}: an option's fields"
    crossed = _every_name(sheet) & _OF_THE_KEY
    assert not crossed, f"step {step}: the key crossed the structure route: {crossed}"


async def _tree(
    client: Client, course_id: uuid.UUID, bearer: dict[str, str], step: int
) -> tuple[set[str], list[tuple[str, str]]]:
    """The student's tree: the nodes it shows, and its tests with their labels."""
    response = await client.ac.get(
        f"/api/v1/portal/courses/{course_id}/materials", headers=bearer
    )
    assert response.status_code == 200, f"step {step}: {response.text}"
    nodes: set[str] = set()
    tests: list[tuple[str, str]] = []

    def walk(node: dict[str, Any]) -> None:
        nodes.add(node["id"])
        tests.extend(
            (item["id"], item["label"])
            for item in node["documents"]
            if item["task_type"] == "test"
        )
        for child in node["children"]:
            walk(child)

    walk(response.json())
    return nodes, tests


async def _structure(
    client: Client, test_id: uuid.UUID, bearer: dict[str, str], step: int
) -> dict[str, Any]:
    response = await client.ac.get(
        f"/api/v1/portal/tasks/{test_id}/test", headers=bearer
    )
    assert response.status_code == 200, f"step {step}: {response.text}"
    sheet: dict[str, Any] = response.json()
    return sheet


async def _review(
    client: Client, submission_id: uuid.UUID, bearer: dict[str, str]
) -> dict[str, Any]:
    response = await client.ac.get(
        f"/api/v1/portal/submissions/{submission_id}", headers=bearer
    )
    assert response.status_code == 200, response.text
    review: dict[str, Any] = response.json()
    return review


@dataclass(frozen=True)
class _Ledger:
    """What a step can leave behind: the tenant's jobs and register rows."""

    jobs: frozenset[uuid.UUID]
    rows: int


def _dropped() -> int:
    """Every register row dropped so far — a call outside a job is one."""
    return sum(skipped_write_counts().values())


async def _ledger(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> _Ledger:
    async with session_factory() as session:
        jobs = frozenset(
            (
                await session.execute(select(Job.id).where(Job.tenant_id == tenant_id))
            ).scalars()
        )
    return _Ledger(jobs=jobs, rows=await _tenant_rows(session_factory, tenant_id))


async def _new_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    before: _Ledger,
) -> list[Job]:
    """The tenant's jobs that were not there before, oldest first."""
    async with session_factory() as session:
        result = await session.execute(
            select(Job)
            .where(Job.tenant_id == tenant_id, Job.id.not_in(before.jobs))
            .order_by(Job.queued_at)
        )
        return list(result.scalars())


async def _nothing_asked(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    before: _Ledger,
    dropped: int,
    step: int,
) -> None:
    """No job, no register row, no row dropped: nothing ran, nothing was asked for."""
    after = await _ledger(session_factory, tenant_id)
    assert after.jobs == before.jobs, f"step {step}: no job"
    assert after.rows == before.rows, f"step {step}: no register row"
    assert _dropped() == dropped, f"step {step}: no call outside a job"


async def _queued(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    job_type: JobType,
) -> list[Job]:
    async with session_factory() as session:
        result = await session.execute(
            select(Job)
            .where(
                Job.tenant_id == tenant_id,
                Job.job_type == job_type.value,
                Job.status == "queued",
            )
            .order_by(Job.queued_at)
        )
        return list(result.scalars())


async def _run_submissions(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    client: Client,
) -> list[uuid.UUID]:
    """Run every queued submission the way the worker would; their job ids.

    The worker is handed the walk's one queue, so what a review asks for after
    delivery is recorded beside what the routes put on it.
    """
    jobs = await _queued(session_factory, tenant_id, JobType.HOMEWORK_PROCESSING)
    for job in jobs:
        assert job.input_params is not None, "a submission's job names its submission"
        await arq_process_homework(
            {
                "session_factory": session_factory,
                "stage_router": _NoModel(),
                "s3_client": client.storage,
                "redis": client.queue,
            },
            str(job.id),
            str(job.input_params["submission_id"]),
        )
    return [job.id for job in jobs]


async def _run_explanations(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    model: _ExplanationsModel,
) -> list[uuid.UUID]:
    """Run every queued generation the way the worker would; their job ids."""
    jobs = await _queued(session_factory, tenant_id, JobType.KEY_EXPLANATION)
    for job in jobs:
        assert job.input_params is not None, "a generation's job names its version"
        await arq_explain_key(
            {"session_factory": session_factory, "stage_router": model, "job_try": 1},
            str(job.id),
            str(job.input_params["reference_id"]),
        )
    return [job.id for job in jobs]


async def _rows(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    action: str | None = None,
    other_than: str | None = None,
) -> list[ExternalServiceCall]:
    """The register rows of one job, of one action or of every other one."""
    stmt = select(ExternalServiceCall).where(ExternalServiceCall.job_id == job_id)
    if action is not None:
        stmt = stmt.where(ExternalServiceCall.action == action)
    if other_than is not None:
        stmt = stmt.where(ExternalServiceCall.action != other_than)
    async with session_factory() as session:
        return list((await session.execute(stmt)).scalars())


async def _tenant_rows(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> int:
    """Every register row of this tenant's jobs."""
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ExternalServiceCall)
                .where(
                    ExternalServiceCall.job_id.in_(
                        select(Job.id).where(Job.tenant_id == tenant_id)
                    )
                )
            )
            or 0
        )


async def _generations(
    session_factory: async_sessionmaker[AsyncSession], test_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, str]]:
    """Every generation job of the test: its id, the language and the form it is for."""
    async with session_factory() as session:
        jobs = list(
            (
                await session.execute(
                    select(Job)
                    .where(
                        Job.subject_id == test_id,
                        Job.job_type == JobType.KEY_EXPLANATION.value,
                    )
                    .order_by(Job.queued_at)
                )
            ).scalars()
        )
        found: list[tuple[uuid.UUID, str, str]] = []
        for job in jobs:
            assert job.input_params is not None, "a generation's job names its version"
            reference = await session.get(
                TaskReference, uuid.UUID(str(job.input_params["reference_id"]))
            )
            assert reference is not None
            found.append((job.id, reference.language, reference.source_content_hash))
        return found


async def _versions(
    session_factory: async_sessionmaker[AsyncSession], test_id: uuid.UUID
) -> list[TestVersion]:
    """The test's published versions, by number."""
    async with session_factory() as session:
        result = await session.execute(
            select(TestVersion)
            .where(TestVersion.authored_document_id == test_id)
            .order_by(TestVersion.version)
        )
        return list(result.scalars())


async def _submissions(
    session_factory: async_sessionmaker[AsyncSession], test_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(HomeworkSubmission)
                .where(HomeworkSubmission.authored_document_id == test_id)
            )
            or 0
        )


async def _stored_review(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> tuple[str | None, str | None, int | None]:
    """The stored review: ``review_result`` as the database renders it, and more.

    The markdown and the score beside it: the three a new grading would rewrite.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    cast(HomeworkSubmission.review_result, Text),
                    HomeworkSubmission.review_markdown,
                    HomeworkSubmission.score,
                ).where(HomeworkSubmission.id == submission_id)
            )
        ).one()
    return row[0], row[1], row[2]


async def _bound_version(
    session_factory: async_sessionmaker[AsyncSession],
    storage: _Storage,
    submission_id: uuid.UUID,
) -> uuid.UUID:
    """The version a submission's stored answers name — the one it is taken for."""
    async with session_factory() as session:
        submission = await session.get(HomeworkSubmission, submission_id)
    assert submission is not None
    key = storage.extract_key(submission.file_url)
    assert key is not None
    version_id, _ = read_stored_answers(storage.objects[key].decode())
    return version_id


async def _one_generation(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID, step: int
) -> None:
    """One generation's work: one row of the stage, besides the port's decision."""
    stage = await _rows(session_factory, job_id, action=STAGE_NAME)
    port = await _rows(session_factory, job_id, action=FUNDS_PORT_ACTION)
    other = [
        row.action
        for row in await _rows(session_factory, job_id, other_than=STAGE_NAME)
        if row.action != FUNDS_PORT_ACTION
    ]
    assert len(stage) == 1, f"step {step}: exactly one row of the stage"
    assert len(port) == 1, f"step {step}: the funds port was asked once"
    assert other == [], f"step {step}: no row of another action"


async def _only_in(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    rows_before: int,
    job_ids: list[uuid.UUID],
    step: int,
) -> None:
    """Every register row the step added belongs to the step's own work."""
    added = await _tenant_rows(session_factory, tenant_id) - rows_before
    own = sum([len(await _rows(session_factory, job_id)) for job_id in job_ids])
    assert added == own, f"step {step}: a register row outside the step's own work"


async def _one_submission_without_a_call(
    session_factory: async_sessionmaker[AsyncSession], ran: list[uuid.UUID], step: int
) -> None:
    """One job ran; the register holds its port's decision and nothing else."""
    assert len(ran) == 1, f"step {step}: one submission's work ran"
    (job_id,) = ran
    calls = await _rows(session_factory, job_id, other_than=FUNDS_PORT_ACTION)
    port = await _rows(session_factory, job_id, action=FUNDS_PORT_ACTION)
    assert calls == [], f"step {step}: no model call in the submission's work"
    assert [(r.action, r.cost_usd) for r in port] == [(FUNDS_PORT_ACTION, None)], (
        f"step {step}: one row of the funds port's decision, without a price"
    )


class TestAcceptance:
    async def test_test_as_an_author_object_end_to_end(
        self,
        client: Client,
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole of acceptance criterion 2, in one walk of eleven steps.

        Each step is measured before and after — the jobs, the register rows per
        job and per action, and the rows dropped — so a step that silently began
        to call a model, or stopped asking for what it must ask for, fails here
        rather than on a live run with a real invoice.
        """
        ac, storage, queue = client.ac, client.storage, client.queue
        tenant_id, course_id = world["tenant_id"], world["course_id"]
        section_id = world["section_id"]
        model = _ExplanationsModel(session_factory)
        prep, check = _key(tenant_id, "prep"), _key(tenant_id, "check")
        bearer = await _student_session(client, world)

        # No job context leaks in from elsewhere: a register row written under
        # a stray one would count against the wrong job (hot fix 4).
        assert get_current_job_id() is None
        dropped = _dropped()

        with (
            patch(_SWITCHES[0], return_value=_config()),
            patch(_SWITCHES[1], return_value=_config()),
            patch(
                "course_supporter.homework.webhook.deliver_webhook",
                new=AsyncMock(return_value=True),
            ),
        ):
            # ── 1. The author uploads the YAML file: a draft, and nothing else
            client.use_key(prep)
            before = await _ledger(session_factory, tenant_id)
            objects_before = dict(storage.objects)
            uploaded = await ac.post(
                f"/api/v1/nodes/{section_id}/documents",
                data={"source_type": "code", "task_type": "test"},
                files={"file": ("lecture-3-test.yaml", _YAML, "application/yaml")},
            )
            assert uploaded.status_code == 201, f"step 1: {uploaded.text}"
            created = uploaded.json()
            assert (created["title"], created["job_id"]) == (_TITLE, None), "step 1"
            test_id = uuid.UUID(created["id"])
            read = await ac.get(f"/api/v1/tests/{test_id}/draft")
            assert read.status_code == 200, f"step 1: {read.text}"
            draft = read.json()
            assert draft["published"] is None, "step 1: a draft, not published"
            assert draft["draft"]["pass_threshold"] == 80, "step 1: the file's mark"
            assert {
                q["number"]: [o["label"] for o in q["options"] if o["correct"]]
                for q in draft["draft"]["questions"]
            } == _KEY, "step 1: the file's key, in the course's letters"
            assert await _versions(session_factory, test_id) == [], "step 1: no version"
            assert storage.objects == objects_before, "step 1: nothing stored"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 1)

            # ── 2. Before publication: no structure, no submission, no tree item
            before = await _ledger(session_factory, tenant_id)
            refused = [
                await ac.get(f"/api/v1/portal/tasks/{test_id}/test", headers=bearer),
                await ac.post(
                    f"/api/v1/portal/tasks/{test_id}/test-submissions",
                    json={"answers": _KEY},
                    headers=bearer,
                ),
            ]
            client.use_key(check)
            refused += [
                await ac.get(f"/api/v1/homework/tasks/{test_id}/test"),
                await ac.post(
                    "/api/v1/homework/submit-test",
                    json={
                        "student_external_id": _EXTERNAL_ID,
                        "course_node_id": str(course_id),
                        "node_id": str(section_id),
                        "authored_document_id": str(test_id),
                        "answers": _KEY,
                    },
                ),
            ]
            for response in refused:
                assert (response.status_code, response.json()["detail"]) == (
                    404,
                    MISSING_TASK,
                ), f"step 2: {response.request.method} {response.request.url.path}"
            nodes, tests = await _tree(client, course_id, bearer, step=2)
            assert str(section_id) in nodes, "step 2: the tree shows the section"
            assert tests == [], "step 2: no test in the student's tree"
            assert await _submissions(session_factory, test_id) == 0, "step 2: no row"
            assert storage.objects == objects_before, "step 2: nothing stored"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 2)

            # ── 3. The author publishes: version 1, and exactly one generation
            client.use_key(prep)
            before = await _ledger(session_factory, tenant_id)
            published = await ac.post(f"/api/v1/tests/{test_id}/publish")
            assert published.status_code == 201, f"step 3: {published.text}"
            first = published.json()
            assert (first["created"], first["published"]["number"]) == (True, 1), (
                "step 3: the first version"
            )
            version_1 = first["published"]["version"]
            asked = await _new_jobs(session_factory, tenant_id, before)
            assert [job.job_type for job in asked] == [JobType.KEY_EXPLANATION.value], (
                "step 3: exactly one job, the explanations'"
            )
            assert [
                (language, form)
                for _, language, form in await _generations(session_factory, test_id)
            ] == [("ukr", version_1)], "step 3: in the course language, for version 1"
            model.explanations = dict(_MODEL_1)
            rows_before = await _tenant_rows(session_factory, tenant_id)
            (generation,) = await _run_explanations(session_factory, tenant_id, model)
            assert generation == asked[0].id, (
                "step 3: the job the publication asked for"
            )
            await _one_generation(session_factory, generation, step=3)
            await _only_in(session_factory, tenant_id, rows_before, [generation], 3)
            assert _dropped() == dropped, "step 3: no call outside a job"

            # ── 4. The student's view: the tree names it; the form reads no key
            before = await _ledger(session_factory, tenant_id)
            _, tests = await _tree(client, course_id, bearer, step=4)
            assert tests == [(str(test_id), _TITLE)], "step 4: in the tree, by name"
            sheet = await _structure(client, test_id, bearer, step=4)
            _the_form_reads(sheet, step=4)
            assert (sheet["version"], sheet["accepting_answers"]) == (
                version_1,
                True,
            ), "step 4: version 1's form, taking answers"
            assert [q["number"] for q in sheet["questions"]] == list(_KEY), "step 4"
            assert [
                (o["label"], o["text"]) for o in sheet["questions"][4]["options"]
            ] == [
                ("а", "«Виправ помилки»"),
                ("б", "«Зроби краще»"),
                ("в", "«Додай у parse_date перевірку порожнього рядка і тест на неї»"),
            ], "step 4: question 5 as the file wrote it, lettered by the course"
            flat = json.dumps(sheet, ensure_ascii=False)
            for secret in (_AUTHOR_ON_3, *_MODEL_1.values()):
                assert secret not in flat, f"step 4: {secret!r} crossed the route"
            client.use_key(check)
            channel = await ac.get(f"/api/v1/homework/tasks/{test_id}/test")
            assert channel.json() == sheet, "step 4: the channel reads the same"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 4)

            # ── 5. The student answers as the form does, wrong on 5: no call ──
            rows_before = await _tenant_rows(session_factory, tenant_id)
            answered = await ac.post(
                f"/api/v1/portal/tasks/{test_id}/test-submissions",
                json=_as_the_form_sends(sheet, _WRONG_ON_5),
                headers=bearer,
            )
            assert answered.status_code == 202, f"step 5: {answered.text}"
            old_attempt = uuid.UUID(answered.json()["submission_id"])
            ran = await _run_submissions(session_factory, tenant_id, client)
            await _one_submission_without_a_call(session_factory, ran, step=5)
            await _only_in(session_factory, tenant_id, rows_before, ran, 5)
            assert _dropped() == dropped, "step 5: no call outside a job"
            old_view = await _review(client, old_attempt, bearer)
            assert (old_view["status"], old_view["score"]) == ("completed", 80), (
                "step 5: four of five"
            )
            markdown = old_view["review_markdown"]
            assert "## Зараховано" in markdown, "step 5: passed at the mark of 80"
            assert "**Питання 5:** Неправильно" in markdown, "step 5: wrong on 5"
            assert f"- **Правильна відповідь:** {_RIGHT_ON_5}" in markdown, "step 5"
            assert f"- {_MODEL_1['5']}" in markdown, "step 5: the model explains 5"
            for number in ("1", "2", "3", "4"):
                assert f"**Питання {number}:** Правильно" in markdown, (
                    f"step 5: right on {number}"
                )
            assert [
                (language, form)
                for _, language, form in await _generations(session_factory, test_id)
            ] == [("ukr", version_1)], (
                "step 5: nothing asked for — version 1's explanations are ready"
            )
            old_review = await _stored_review(session_factory, old_attempt)
            assert None not in old_review, "step 5: the review is stored whole"

            # ── 6. A question is added and published: one version, one job ────
            client.use_key(prep)
            before = await _ledger(session_factory, tenant_id)
            read = await ac.get(f"/api/v1/tests/{test_id}/draft")
            body = _written_back(read.json()["draft"])
            body["questions"].append(_ADDED)
            edited = await ac.put(f"/api/v1/tests/{test_id}/draft", json=body)
            assert edited.status_code == 200, f"step 6: {edited.text}"
            assert edited.json()["published"]["number"] == 1, "step 6: not published"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 6)
            published = await ac.post(f"/api/v1/tests/{test_id}/publish")
            assert published.status_code == 201, f"step 6: {published.text}"
            second = published.json()
            assert (second["created"], second["published"]["number"]) == (True, 2), (
                "step 6: a new version"
            )
            version_2 = second["published"]["version"]
            assert version_2 != version_1, "step 6: another form"
            asked = await _new_jobs(session_factory, tenant_id, before)
            assert [job.job_type for job in asked] == [JobType.KEY_EXPLANATION.value], (
                "step 6: exactly one new job, the explanations'"
            )
            assert [
                (language, form)
                for _, language, form in await _generations(session_factory, test_id)
            ] == [("ukr", version_1), ("ukr", version_2)], "step 6: for version 2"
            model.explanations = dict(_MODEL_2)
            rows_before = await _tenant_rows(session_factory, tenant_id)
            (generation,) = await _run_explanations(session_factory, tenant_id, model)
            assert generation == asked[0].id, (
                "step 6: the job the publication asked for"
            )
            await _one_generation(session_factory, generation, step=6)
            await _only_in(session_factory, tenant_id, rows_before, [generation], 6)
            assert _dropped() == dropped, "step 6: no call outside a job"

            # ── 7. The old attempt's review stands, byte for byte ─────────────
            assert await _stored_review(session_factory, old_attempt) == old_review, (
                "step 7: review_result, review_markdown and score as they were"
            )
            assert await _review(client, old_attempt, bearer) == old_view, (
                "step 7: the student reads the same review"
            )
            assert _dropped() == dropped, "step 7: no call outside a job"

            # ── 8. A new attempt in English, taken for version 2 ──────────────
            sheet = await _structure(client, test_id, bearer, step=8)
            _the_form_reads(sheet, step=8)
            assert (sheet["version"], len(sheet["questions"])) == (version_2, 6), (
                "step 8: the form of version 2"
            )
            answered = await ac.post(
                f"/api/v1/portal/tasks/{test_id}/test-submissions",
                json=_as_the_form_sends(sheet, _NEW_ATTEMPT, language="eng"),
                headers=bearer,
            )
            assert answered.status_code == 202, f"step 8: {answered.text}"
            new_attempt = uuid.UUID(answered.json()["submission_id"])
            versions = await _versions(session_factory, test_id)
            assert [v.version for v in versions] == [1, 2], "step 8: two versions"
            assert (
                await _bound_version(session_factory, storage, new_attempt)
                == versions[1].id
            ), "step 8: the answers are taken for version 2"

            # ── 9. While it waits, the pass mark alone changes: version 3 ─────
            client.use_key(prep)
            before = await _ledger(session_factory, tenant_id)
            read = await ac.get(f"/api/v1/tests/{test_id}/draft")
            body = _written_back(read.json()["draft"])
            body["pass_threshold"] = _STRICTER
            edited = await ac.put(f"/api/v1/tests/{test_id}/draft", json=body)
            assert edited.status_code == 200, f"step 9: {edited.text}"
            published = await ac.post(f"/api/v1/tests/{test_id}/publish")
            assert published.status_code == 201, f"step 9: {published.text}"
            third = published.json()
            assert (third["created"], third["published"]["number"]) == (True, 3), (
                "step 9: a new version"
            )
            assert third["published"]["version"] == version_2, (
                "step 9: the same `version` — the form did not change"
            )
            await _nothing_asked(session_factory, tenant_id, before, dropped, 9)
            assert (await _structure(client, test_id, bearer, step=9))[
                "version"
            ] == version_2, "step 9: the student's form stands"

            # ── 8, graded: its work runs only now, with version 3 in force ────
            rows_before = await _tenant_rows(session_factory, tenant_id)
            generations_before = await _generations(session_factory, test_id)
            ran = await _run_submissions(session_factory, tenant_id, client)
            await _one_submission_without_a_call(session_factory, ran, step=8)
            await _only_in(session_factory, tenant_id, rows_before, ran, 8)
            assert _dropped() == dropped, "step 8: no call outside a job"
            review = await _review(client, new_attempt, bearer)
            assert (review["status"], review["score"]) == ("completed", 83), (
                "step 8: five of six"
            )
            markdown = review["review_markdown"]
            assert "**Question 6:** Correct" in markdown, "step 8: the added question"
            assert "**Question 5:** Incorrect" in markdown, "step 8: wrong on 5"
            assert f"- {_MODEL_2['5']}" in markdown, "step 8: version 2's explanations"
            assert "The explanation is provided in the course language." in markdown, (
                "step 8: in the course language, and the line that says so"
            )
            assert "## Passed" in markdown, (
                "step 8: graded by version 2 — its pass mark of 80, not version 3's 90"
            )
            asked_after = [
                (language, form)
                for job_id, language, form in await _generations(
                    session_factory, test_id
                )
                if (job_id, language, form) not in generations_before
            ]
            assert asked_after == [("eng", version_2)], (
                "step 8: after delivery, one generation asked for, for (version 2, eng)"
            )

            # ── 10. The same draft published again: no version, no job ────────
            before = await _ledger(session_factory, tenant_id)
            again = await ac.post(f"/api/v1/tests/{test_id}/publish")
            assert again.status_code == 200, f"step 10: {again.text}"
            assert again.json() == {
                "created": False,
                "published": third["published"],
            }, "step 10: the version in force, returned"
            assert [v.version for v in await _versions(session_factory, test_id)] == [
                1,
                2,
                3,
            ], "step 10: no new version"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 10)

            # ── 11. Every job went to a queue whose worker runs it — the
            #        publications' and the review's after delivery alike ────────
            served = _served()
            put = [
                (call.args[0], call.kwargs.get("_queue_name"))
                for call in queue.enqueue_job.await_args_list
            ]
            stranded = [
                (function, name)
                for function, name in put
                if function not in served.get(name or "", frozenset())
            ]
            assert not stranded, f"step 11: on a queue that does not run it: {stranded}"
            explanations = [
                call
                for call in queue.enqueue_job.await_args_list
                if call.args[0] == "arq_explain_key"
            ]
            generations = await _generations(session_factory, test_id)
            assert [(language, form) for _, language, form in generations] == [
                ("ukr", version_1),
                ("ukr", version_2),
                ("eng", version_2),
            ], "step 11: the walk's three generations"
            assert [call.args[1] for call in explanations] == [
                str(job_id) for job_id, _, _ in generations
            ], "step 11: each generation's job put on a queue once"
            assert {call.kwargs.get("_queue_name") for call in explanations} == {
                default_queue_name
            }, "step 11: the explanations on arq's default queue"
            submissions = [
                name for function, name in put if function == "arq_process_homework"
            ]
            assert submissions == [HomeworkWorkerSettings.queue_name] * 2, (
                "step 11: the two submissions on their own queue"
            )
