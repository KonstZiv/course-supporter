"""Acceptance of task 07, walked end to end (mentor-rebuild tasks 07, 07b).

The criterion this file exists for is a THROUGH one — "a test answered with its
answers is reviewed without a model call, its explanations are written once
per version and language, and what cannot be answered is refused at the door"
— and a through criterion is not proved by a pile of unit tests, each of which
can be green while the joints between them are broken (``vision-rules#25``).

So nothing here is called directly but one thing. Since task 07b the test is
written in the system and its key is its published version's; the author's
routes that publish it come with commit E1, so until then the author publishes
through the test's service — the one direct call. The key is read back through
the author's route with a key of scope PREP; the student answers through the
portal with a real bearer session from a real login; the channel answers with
a key of scope CHECK. Every submission is stored by the core in the storage
double and read back from it by the real ARQ body, which the test runs itself,
as the worker would. The database is live. The MODEL is the one double of the
explanations' stage, and it writes the register row a real call would have
written — so "no call" and "one call" below are counts over real rows, per job
and per action, before and after every step.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.agents.key_explainer import STAGE_NAME
from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.api.tasks import arq_process_homework
from course_supporter.auth.context import TenantContext
from course_supporter.funds_port import FUNDS_PORT_ACTION
from course_supporter.homework.explanation_queue import ArqExplanationQueue
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    SubmissionState,
)
from course_supporter.homework.test_object import (
    DraftBody,
    DraftOption,
    DraftQuestion,
)
from course_supporter.homework.test_object_service import (
    Publication,
    TestObjectService,
)
from course_supporter.homework.test_text import parse_test
from course_supporter.jobs import JobType
from course_supporter.service_logging import get_current_job_id
from course_supporter.storage.content_hash import compute_content_hash
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    StudentCredential,
    StudentCredentialToken,
    StudentEnrollment,
    TaskReference,
    Tenant,
)
from course_supporter.storage.test_object_repository import TestObjectRepository
from course_supporter.workers.key_explain import arq_explain_key
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_SOURCE = Path(__file__).parents[1] / "fixtures" / "reference" / "test_source.md"
_TEXT = _SOURCE.read_text(encoding="utf-8")
_KEY: dict[str, list[str]] = {
    "1": ["б"],
    "2": ["в"],
    "3": ["а"],
    "4": ["г"],
    "5": ["в"],
}
_PASS_MARK = 80
_AUTHOR_ON_3 = "Тести одразу показують, чи не зламала зміна агента те, що працювало."
_MODEL_UKR = {number: f"Пояснення моделі до питання {number}." for number in _KEY}
_MODEL_ENG = {
    number: f"The model's explanation of question {number}." for number in _KEY
}
_WRONG_ON_5 = {**_KEY, "5": ["а"]}
_WRONG_ON_2_AND_3 = {**_KEY, "2": ["а"], "3": ["б"]}
_RIGHT_ON_5 = (
    "в) «Додай у функцію parse_date перевірку порожнього рядка і тест на цей випадок»"
)
_LOGIN = "e2e-test-student"
_PASSWORD = "correct horse 07"
_EXTERNAL_ID = "channel-e2e-test-student"
_SWITCHES = (
    "course_supporter.homework.test_doors.get_path_config",
    "course_supporter.homework.path_runner.get_path_config",
)


def _draft() -> DraftBody:
    """The fixture's five questions as the author writes them in the system.

    Read out of the fixture file, not retyped: the right options are the key's,
    the pass mark is 80, and question 3 carries the author's own words.
    """
    return DraftBody(
        pass_threshold=_PASS_MARK,
        questions=tuple(
            DraftQuestion(
                text=question.text,
                options=tuple(
                    DraftOption(
                        text=option.text,
                        correct=option.label in _KEY[question.number],
                    )
                    for option in question.options
                ),
                explanation=_AUTHOR_ON_3 if question.number == "3" else None,
            )
            for question in parse_test(_TEXT).questions
        ),
    )


async def _publish(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    test_id: uuid.UUID,
    draft: DraftBody | None = None,
) -> Publication:
    """The author publishes — through the service until commit E1 gives a route.

    The queue is the real one over a double of Redis, as the author's routes
    wire it: the explanations' job is a real row, which the test then runs.
    """
    async with session_factory() as session:
        service = TestObjectService(
            session,
            ArqExplanationQueue(
                redis=AsyncMock(enqueue_job=AsyncMock(return_value=None)),
                session=session,
                tenant_id=tenant_id,
            ),
        )
        if draft is not None:
            await service.save_draft(test_id, draft)
        publication = await service.publish(test_id)
        await session.commit()
    return publication


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
        self.doubts: dict[str, bool] = {}

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
                        "doubts": {
                            n: self.doubts.get(n, False) for n in self.explanations
                        },
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
        tenant_name="e2e-test",
        scopes=list(scopes),
        plan_id="basic",
        key_prefix="cs_e2e07",
    )


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """A course in Ukrainian with the test of five questions, and its student.

    The test is written in the system (task 07b): the fixture file's questions
    as its draft, not yet published — publishing it is the author's first step.
    """
    async with session_factory() as session:
        tenant = Tenant(
            name=f"e2e-test-{uuid.uuid4().hex[:8]}",
            webhook_url="https://channel.example/hook",
        )
        session.add(tenant)
        await session.flush()
        course = make_root_course_node(tenant_id=tenant.id, title="E2E 07", order=0)
        session.add(course)
        await session.flush()
        test = AuthoredDocument(
            course_node_id=course.id,
            course_root_id=course.id,
            source_type="test_object",
            source_url="test-object:",
            order=1,
            task_type="test",
            language="ukr",
            content_hash=compute_content_hash(b"", []),
            title="Тест: основи роботи з агентом",
        )
        session.add(test)
        await session.flush()
        await TestObjectRepository(session).replace_draft(test.id, _draft().to_jsonb())
        student = Student(tenant_id=tenant.id, external_id=_EXTERNAL_ID)
        session.add(student)
        await session.flush()
        session.add(StudentEnrollment(student_id=student.id, course_node_id=course.id))
        await session.commit()
        ids = {
            "tenant_id": tenant.id,
            "course_id": course.id,
            "test_id": test.id,
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


@pytest.fixture()
async def client(
    world: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> AsyncGenerator[tuple[AsyncClient, Callable[[TenantContext], None], _Storage]]:
    """A live client, a switch for the next request's key, and the storage.

    ``get_current_student`` is NOT overridden: the portal half runs the real
    bearer flow. The queue is a double whose only job is not to dispatch — the
    ``Job`` rows are the real ones, and the test runs the work itself.
    """
    storage = _Storage(tmp_path)

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_arq_redis] = lambda: AsyncMock(
        enqueue_job=AsyncMock(return_value=None)
    )
    app.dependency_overrides[get_s3_client] = lambda: storage
    app.dependency_overrides[get_current_tenant] = lambda: _key(
        world["tenant_id"], "prep"
    )

    def use_key(ctx: TenantContext) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: ctx

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, use_key, storage
    app.dependency_overrides.clear()


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
    storage: _Storage,
) -> list[uuid.UUID]:
    """Run every queued submission the way the worker would; their job ids."""
    jobs = await _queued(session_factory, tenant_id, JobType.HOMEWORK_PROCESSING)
    for job in jobs:
        await arq_process_homework(
            {
                "session_factory": session_factory,
                "stage_router": _NoModel(),
                "s3_client": storage,
                "redis": AsyncMock(enqueue_job=AsyncMock(return_value=None)),
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
) -> list[tuple[uuid.UUID, str | None, str]]:
    """Every generation job of the test: its id, the version's language and text."""
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
        found = []
        for job in jobs:
            version = await session.get(
                TaskReference, uuid.UUID(str(job.input_params["reference_id"]))
            )
            assert version is not None
            found.append((job.id, version.language, version.source_content_hash))
        return found


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
    async def test_test_as_submission_end_to_end(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None], _Storage],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole of acceptance criterion 2, in one walk of eight steps.

        Each step is measured against the register before and after, per job
        and per action, so a step that silently started costing anything — or
        stopped asking for what it must ask for — fails here rather than on a
        live run with a real invoice.
        """
        ac, use_key, storage = client
        tenant_id, test_id = world["tenant_id"], world["test_id"]
        model = _ExplanationsModel(session_factory)
        reference = f"/api/v1/documents/{test_id}/reference"
        delivered = AsyncMock(return_value=True)

        # Both sides of every comparison below are non-empty
        # (``vision-rules#16``), and no job context leaks in from elsewhere: a
        # register row written under a stray one would count against the wrong
        # job (the lesson of hot fix 4).
        assert len(_KEY) == 5, "the key under test covers the five questions"
        assert get_current_job_id() is None

        with (
            patch(_SWITCHES[0], return_value=_config()),
            patch(_SWITCHES[1], return_value=_config()),
            patch("course_supporter.homework.webhook.deliver_webhook", new=delivered),
        ):
            # ── 1. The author publishes: the key, pass mark 80, own words on 3
            use_key(_key(tenant_id, "prep"))
            rows_before = await _tenant_rows(session_factory, tenant_id)
            publication = await _publish(session_factory, tenant_id, test_id)
            assert publication.created, "step 1: the first version"
            digest = publication.version.content_digest
            model.explanations, model.doubts = dict(_MODEL_UKR), {"2": True}
            (generation,) = await _run_explanations(session_factory, tenant_id, model)
            await _one_generation(session_factory, generation, step=1)
            await _only_in(session_factory, tenant_id, rows_before, [generation], 1)
            read = (await ac.get(reference)).json()
            assert (read["status"], read["language"]) == ("ready", "ukr"), "step 1"
            assert read["pass_threshold"] == _PASS_MARK, "step 1: the pass mark"
            assert read["doubts"].get("2") is True, "step 1: the doubt on 2 is stored"

            # ── 2. The channel reads the test: three fields, nothing of the key
            use_key(_key(tenant_id, "check"))
            rows_before = await _tenant_rows(session_factory, tenant_id)
            shown = await ac.get(f"/api/v1/homework/tasks/{test_id}/test")
            assert shown.status_code == 200, shown.text
            sheet = shown.json()
            assert set(sheet) == {"version", "accepting_answers", "questions"}, (
                "step 2: the three fields"
            )
            assert (sheet["version"], sheet["accepting_answers"]) == (digest, True)
            assert [q["number"] for q in sheet["questions"]] == list(_KEY)
            flat = json.dumps(sheet, ensure_ascii=False)
            for word in ('"answers"', '"explanations"', '"doubts"', _MODEL_UKR["1"]):
                assert word not in flat, f"step 2: {word} crossed the structure route"
            await _only_in(session_factory, tenant_id, rows_before, [], 2)

            # ── 3. The student answers in the portal, wrong on 5 ────────────
            use_key(_key(tenant_id, "prep"))
            provision = await ac.post(
                "/api/v1/students",
                json={
                    "mode": "existing",
                    "student_id": str(world["student_id"]),
                    "login": _LOGIN,
                    "password": _PASSWORD,
                },
            )
            assert provision.status_code == 201, provision.text
            login = await ac.post(
                "/api/v1/portal/login",
                json={
                    "tenant_id": str(tenant_id),
                    "login": _LOGIN,
                    "password": _PASSWORD,
                },
            )
            assert login.status_code == 200, login.text
            bearer = {"Authorization": f"Bearer {login.json()['access_token']}"}

            rows_before = await _tenant_rows(session_factory, tenant_id)
            answered = await ac.post(
                f"/api/v1/portal/tasks/{test_id}/test-submissions",
                json={"answers": _WRONG_ON_5, "test_version": digest},
                headers=bearer,
            )
            assert answered.status_code == 202, answered.text
            ran = await _run_submissions(session_factory, tenant_id, storage)
            await _one_submission_without_a_call(session_factory, ran, step=3)
            await _only_in(session_factory, tenant_id, rows_before, ran, 3)
            review = (
                await ac.get(
                    f"/api/v1/portal/submissions/{answered.json()['submission_id']}",
                    headers=bearer,
                )
            ).json()
            assert (review["status"], review["score"]) == ("completed", 80), "step 3"
            markdown = review["review_markdown"]
            assert "## Зараховано" in markdown, "step 3: passed"
            assert "Бал: 80 %" in markdown, "step 3: the score"
            assert "**Питання 5:** Неправильно" in markdown, "step 3: wrong on 5"
            assert f"- **Правильна відповідь:** {_RIGHT_ON_5}" in markdown, (
                "step 3: the right option of 5, as the student saw it"
            )
            assert f"- {_MODEL_UKR['5']}" in markdown, "step 3: the model explains 5"
            for number in ("1", "2", "3", "4"):
                assert f"**Питання {number}:** Правильно" in markdown, (
                    f"step 3: right on {number}"
                )
            assert "Пояснення подано мовою курсу." not in markdown, (
                "step 3: the course language needs no line in the course language"
            )
            assert len(await _generations(session_factory, test_id)) == 1, (
                "step 3: nothing asked for in the course language"
            )

            # ── 4. Wrong on 2 (doubted) and on 3 (the author's words) ───────
            rows_before = await _tenant_rows(session_factory, tenant_id)
            answered = await ac.post(
                f"/api/v1/portal/tasks/{test_id}/test-submissions",
                json={"answers": _WRONG_ON_2_AND_3},
                headers=bearer,
            )
            assert answered.status_code == 202, answered.text
            ran = await _run_submissions(session_factory, tenant_id, storage)
            await _one_submission_without_a_call(session_factory, ran, step=4)
            await _only_in(session_factory, tenant_id, rows_before, ran, 4)
            markdown = (
                await ac.get(
                    f"/api/v1/portal/submissions/{answered.json()['submission_id']}",
                    headers=bearer,
                )
            ).json()["review_markdown"]
            assert (
                "**Питання 2:** Неправильно\n\n"
                "- **Правильна відповідь:** в) Перевірити, що бібліотека справді "
                "існує і має потрібну функцію\n"
                "- Пояснення до цього питання немає."
            ) in markdown, "step 4: a doubt leaves the verdict and no model words"
            assert _MODEL_UKR["2"] not in markdown, "step 4: the doubted words"
            assert f"- {_AUTHOR_ON_3}" in markdown, "step 4: the author's words on 3"
            assert _MODEL_UKR["3"] not in markdown, "step 4: the author's words win"

            # ── 5. The channel answers in English, wrong on 5 ───────────────
            use_key(_key(tenant_id, "check"))
            rows_before = await _tenant_rows(session_factory, tenant_id)
            generations_before = await _generations(session_factory, test_id)
            channel_body = {
                "student_external_id": _EXTERNAL_ID,
                "course_node_id": str(world["course_id"]),
                "node_id": str(world["course_id"]),
                "authored_document_id": str(test_id),
                "answers": _WRONG_ON_5,
                "test_version": digest,
                "response_language": "eng",
            }
            answered = await ac.post("/api/v1/homework/submit-test", json=channel_body)
            assert answered.status_code == 202, answered.text
            ran = await _run_submissions(session_factory, tenant_id, storage)
            await _one_submission_without_a_call(session_factory, ran, step=5)
            await _only_in(session_factory, tenant_id, rows_before, ran, 5)
            payload = delivered.await_args.kwargs["payload"]
            assert payload.event == "reviewed", "step 5: delivered by webhook"
            text = payload.review.review_text
            assert "**Question 5:** Incorrect" in text, "step 5: in English"
            assert f"- {_MODEL_UKR['5']}" in text, "step 5: the course language's words"
            assert "The explanation is provided in the course language." in text, (
                "step 5: the line that says so"
            )
            asked = [
                generation
                for generation in await _generations(session_factory, test_id)
                if generation not in generations_before
            ]
            assert [(language, version) for _, language, version in asked] == [
                ("eng", digest)
            ], "step 5: exactly one generation asked for, for (version, eng)"

            # ── 6. It runs; the second English answer reads English ─────────
            model.explanations, model.doubts = dict(_MODEL_ENG), {}
            rows_before = await _tenant_rows(session_factory, tenant_id)
            (english,) = await _run_explanations(session_factory, tenant_id, model)
            assert english == asked[0][0], "step 6: the generation step 5 asked for"
            await _one_generation(session_factory, english, step=6)

            answered = await ac.post("/api/v1/homework/submit-test", json=channel_body)
            assert answered.status_code == 202, answered.text
            ran = await _run_submissions(session_factory, tenant_id, storage)
            await _one_submission_without_a_call(session_factory, ran, step=6)
            await _only_in(session_factory, tenant_id, rows_before, [english, *ran], 6)
            text = delivered.await_args.kwargs["payload"].review.review_text
            assert f"- {_MODEL_ENG['5']}" in text, "step 6: the English explanation"
            assert "The explanation is provided in the course language." not in text, (
                "step 6: no line about the course language"
            )
            assert len(await _generations(session_factory, test_id)) == 2, (
                "step 6: no new generation"
            )

            # ── 7. A question is added and published: the old form is refused
            rows_before = await _tenant_rows(session_factory, tenant_id)
            extended = DraftBody(
                pass_threshold=_PASS_MARK,
                questions=(
                    *_draft().questions,
                    DraftQuestion(
                        text="Чи варто перевіряти зміни агента?",
                        options=(
                            DraftOption(text="Так", correct=True),
                            DraftOption(text="Ні", correct=False),
                        ),
                    ),
                ),
            )
            renewed = await _publish(session_factory, tenant_id, test_id, extended)
            assert renewed.created, "step 7: the premise, a new version"
            assert renewed.version.content_digest != digest, "step 7: another form"
            submissions_before = await _submissions(session_factory, test_id)
            objects_before = len(storage.objects)
            refused = await ac.post(
                f"/api/v1/portal/tasks/{test_id}/test-submissions",
                json={"answers": _KEY, "test_version": digest},
                headers=bearer,
            )
            assert refused.status_code == 409, refused.text
            assert refused.json()["detail"]["code"] == "TEST_VERSION_CHANGED", "step 7"
            assert await _submissions(session_factory, test_id) == submissions_before, (
                "step 7: no submission row"
            )
            assert len(storage.objects) == objects_before, "step 7: nothing stored"
            await _only_in(session_factory, tenant_id, rows_before, [], 7)

            # ── 8. A file to the test: refused at the file route's door ─────
            use_key(_key(tenant_id, "check"))
            refused = await ac.post(
                "/api/v1/homework/submit",
                data={
                    "student_external_id": _EXTERNAL_ID,
                    "course_node_id": str(world["course_id"]),
                    "node_id": str(world["course_id"]),
                    "authored_document_id": str(test_id),
                },
                files={"file": ("answers.txt", "1: б\n".encode(), "text/plain")},
            )
            assert refused.status_code == 422, refused.text
            assert refused.json()["detail"]["code"] == "TEST_ANSWERS_REQUIRED", "step 8"
            assert await _submissions(session_factory, test_id) == submissions_before, (
                "step 8: no submission row"
            )
            assert len(storage.objects) == objects_before, "step 8: nothing stored"
            await _only_in(session_factory, tenant_id, rows_before, [], 8)
