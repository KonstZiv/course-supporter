"""The new path end to end, on the type whose path makes no model call.

Acceptance criterion 4 (mentor-rebuild task 03): a ``test`` submission walks the
new path from the doors to "reviewed" without a single model call, the funds
port's numbers land in the register under the same path key the checkpoint
carries, and the webhook's shape is untouched. Criterion 3 is here too: with the
switch on today's Mentor the new path is not entered at all.

Since task 07 the walk ends with a review: the test's result builder scores the
answers against the author's key and writes the review before delivery — still
without a model call. So the seed carries what a test needs (its text, a key, a
ready version of explanations) and the submission's file is the canonical
answers the core stores. What happens after delivery (the request for
explanations in the student's language) and when building fails (decision 15)
is here too.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.api.tasks import arq_process_homework
from course_supporter.funds_port import FUNDS_PORT_ACTION
from course_supporter.homework.path_checkpoint import PathCheckpoint
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    SubmissionState,
)
from course_supporter.homework.reference_key import answers_digest
from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.webhook import ReviewSummary
from course_supporter.reference_kinds import ReferenceKind
from course_supporter.security.schemas import SafetyResult
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    TaskReference,
    TaskReferenceOverride,
    Tenant,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository

pytestmark = pytest.mark.requires_db

_WEBHOOK_URL = "https://example.com/hook"
_TEST_TEXT = (
    "1. Перше?\nа) так\nб) ні\n\n2. Друге?\nа) так\nв) ні\n\n3. Третє?\nа) так\nб) ні"
)
_TEXT_HASH = "c1" + "0" * 62
_KEY: dict[str, list[str]] = {"1": ["б"], "2": ["в"], "3": ["а"]}
_PASS_MARK = 60
# The canonical answers the core stores: question 2 answered wrong — 2 of 3,
# a score of 66, above the pass mark.
_ANSWERS = '{"1":["б"],"2":["а"],"3":["а"]}'
_EXPLANATIONS = {
    "1": "Перше пояснення моделі.",
    "2": "Друге пояснення моделі.",
    "3": "Третє пояснення моделі.",
}


def _config(*, test_on_new_path: bool) -> PathConfig:
    """The shipped shape, with ``test`` optionally switched over.

    ``test`` is the type whose every path lists no stage, which is what makes
    this a test of the skeleton and not of the stages.
    """
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


@pytest.fixture()
async def seed(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    async with session_factory() as session:
        tenant = Tenant(name=f"np-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = CourseNode(
            tenant_id=tenant.id, title="Course", order=0, default_language="ukr"
        )
        session.add(node)
        await session.flush()
        task = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="https://example.com/t",
            task_type="test",
            language="ukr",
            content_hash=_TEXT_HASH,
        )
        session.add(task)
        await session.flush()
        await _give_the_test_a_text_and_a_key(session, task)
        student = Student(
            tenant_id=tenant.id, external_id=f"stu-{uuid.uuid4().hex[:6]}"
        )
        session.add(student)
        await session.flush()
        submission = HomeworkSubmission(
            tenant_id=tenant.id,
            student_id=student.id,
            course_node_id=node.id,
            node_id=node.id,
            authored_document_id=task.id,
            file_url="s3://bucket/homework/answers.json",
            file_type="application/json",
            original_filename="answers.json",
            webhook_url=_WEBHOOK_URL,
            response_language="uk",
            status="received",
        )
        session.add(submission)
        await session.flush()
        job = Job(
            tenant_id=tenant.id,
            course_node_id=node.id,
            job_type="homework_processing",
            subject_type="homework_submission",
            subject_id=submission.id,
            input_params={"submission_id": str(submission.id)},
            status="queued",
        )
        session.add(job)
        await session.flush()
        await session.commit()
        ids = {
            "submission_id": submission.id,
            "job_id": job.id,
            "task_id": task.id,
            "tenant_id": tenant.id,
        }

    yield ids

    async with session_factory() as session:
        # The jobs a test's explanations were asked for, or stood in the way.
        await session.execute(
            Job.__table__.delete().where(Job.subject_id == ids["task_id"])
        )
        await session.execute(
            ExternalServiceCall.__table__.delete().where(
                ExternalServiceCall.job_id == ids["job_id"]
            )
        )
        await session.execute(
            HomeworkSubmission.__table__.delete().where(
                HomeworkSubmission.id == ids["submission_id"]
            )
        )
        await session.execute(Job.__table__.delete().where(Job.id == ids["job_id"]))
        await session.commit()


async def _give_the_test_a_text_and_a_key(
    session: AsyncSession, task: AuthoredDocument
) -> None:
    """What a test needs to be reviewed: its text, a key, ready explanations."""
    summary = DocumentSummary(
        authored_document_id=task.id,
        course_root_id=task.course_root_id,
        title="Тест",
        status="ready",
    )
    session.add(summary)
    await session.flush()
    session.add(
        DocumentSegment(
            document_summary_id=summary.id,
            course_root_id=task.course_root_id,
            order=0,
            content=_TEST_TEXT,
            description="the test",
            start_pos=0,
            end_pos=len(_TEST_TEXT),
        )
    )
    repo = TaskReferenceRepository(session)
    await repo.replace_override(
        authored_document_id=task.id,
        kind=ReferenceKind.TEST_KEY,
        answers=_KEY,
        source_content_hash=_TEXT_HASH,
        pass_threshold=_PASS_MARK,
    )
    version, _ = await repo.create_version(
        authored_document_id=task.id,
        kind=ReferenceKind.TEST_KEY,
        source_content_hash=_TEXT_HASH,
        source_task_type="test",
        answers_hash=answers_digest(_KEY),
        language="ukr",
    )
    await repo.mark_ready(version.id, dict(_EXPLANATIONS), doubts={})


def _ctx(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_file: Path,
    *,
    redis: Any = None,
) -> dict[str, Any]:
    s3 = MagicMock()
    s3.extract_key = MagicMock(return_value="homework/answers.json")
    s3.download_file = AsyncMock(return_value=tmp_file)
    ctx: dict[str, Any] = {
        "session_factory": session_factory,
        "stage_router": MagicMock(),
        "s3_client": s3,
    }
    if redis is not None:
        ctx["redis"] = redis
    return ctx


def _answers_file(tmp_path: Path) -> Path:
    """The file the core stored for this submission: its canonical answers."""
    answers = tmp_path / "answers.json"
    answers.write_text(_ANSWERS, encoding="utf-8")
    return answers


class TestTestTypeEndToEnd:
    async def test_walks_the_path_to_reviewed_without_calling_a_model(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        answers = _answers_file(tmp_path)

        with (
            patch(
                "course_supporter.homework.path_runner.get_path_config",
                return_value=_config(test_on_new_path=True),
            ),
            patch(
                "course_supporter.homework.webhook.deliver_webhook",
                new=AsyncMock(return_value=True),
            ),
        ):
            await arq_process_homework(
                _ctx(session_factory, answers),
                str(seed["job_id"]),
                str(seed["submission_id"]),
            )

        async with session_factory() as session:
            submission = await session.get(HomeworkSubmission, seed["submission_id"])
            job = await session.get(Job, seed["job_id"])
            rows = list(
                (
                    await session.execute(
                        select(ExternalServiceCall).where(
                            ExternalServiceCall.job_id == seed["job_id"]
                        )
                    )
                ).scalars()
            )

        assert submission is not None
        assert submission.status == "delivered"
        # Task 07: the builder wrote a review — a version-1 structure with a
        # test section, its markdown and the score — and still called no model.
        assert submission.review_result is not None
        assert submission.review_result["schema_version"] == REVIEW_SCHEMA_VERSION
        assert submission.review_result["verdict"] == {"passed": True, "why": None}
        assert submission.review_result["test"]["score"] == 66
        assert submission.score == 66
        assert submission.review_markdown is not None
        assert "## Питання тесту" in submission.review_markdown

        # Not one register row records a model call.
        assert [r.action for r in rows] == [FUNDS_PORT_ACTION]
        (port_row,) = rows
        assert port_row.provider is None
        assert port_row.model_id is None
        assert port_row.cost_usd is None
        assert port_row.funds_decision == "allowed"
        # A path with no stage has nothing to pay for.
        assert port_row.ceiling_estimate_usd == 0.0

        # The key in the port's row is the key in the checkpoint.
        assert job is not None
        assert job.stage_progress is not None
        checkpoint = PathCheckpoint.from_jsonb(job.stage_progress)
        assert port_row.path_key == str(checkpoint.path_key)
        assert str(checkpoint.path_key) == "test/first"
        assert checkpoint.stages == {}

    async def test_the_webhook_shape_is_untouched(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        """The same fields as ever, now carrying the test's review (task 07)."""
        answers = _answers_file(tmp_path)
        delivered = AsyncMock(return_value=True)

        with (
            patch(
                "course_supporter.homework.path_runner.get_path_config",
                return_value=_config(test_on_new_path=True),
            ),
            patch("course_supporter.homework.webhook.deliver_webhook", new=delivered),
        ):
            await arq_process_homework(
                _ctx(session_factory, answers),
                str(seed["job_id"]),
                str(seed["submission_id"]),
            )

        payload = delivered.await_args.kwargs["payload"]
        assert payload.event == "reviewed"
        assert set(payload.review.model_dump()) == set(ReviewSummary.model_fields)
        assert payload.review.score == 66
        assert payload.review.passed is True
        assert payload.review.correctness == "partially_correct"
        assert "## Питання тесту" in payload.review.review_text
        assert payload.structure is not None
        assert payload.structure.test is not None
        assert payload.structure.test.score == 66


def _on_the_new_path(delivered: AsyncMock) -> Any:
    """The two patches every walk below needs: the switch, and a delivery."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "course_supporter.homework.path_runner.get_path_config",
            return_value=_config(test_on_new_path=True),
        )
    )
    stack.enter_context(
        patch("course_supporter.homework.webhook.deliver_webhook", new=delivered)
    )
    return stack


async def _read_back(
    session_factory: async_sessionmaker[AsyncSession], seed: dict[str, uuid.UUID]
) -> tuple[HomeworkSubmission, Job]:
    async with session_factory() as session:
        submission = await session.get(HomeworkSubmission, seed["submission_id"])
        job = await session.get(Job, seed["job_id"])
    assert submission is not None
    assert job is not None
    return submission, job


async def _explanation_jobs(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> list[Job]:
    async with session_factory() as session:
        result = await session.execute(
            select(Job).where(
                Job.subject_id == task_id, Job.job_type == "key_explanation"
            )
        )
        return list(result.scalars())


async def _versions(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    **axes: str,
) -> list[TaskReference]:
    async with session_factory() as session:
        stmt = select(TaskReference).where(
            TaskReference.authored_document_id == task_id
        )
        for name, value in axes.items():
            stmt = stmt.where(getattr(TaskReference, name) == value)
        return list((await session.execute(stmt)).scalars())


class TestWhenTheResultCannotBeBuilt:
    """Task 07, decision 15: the new path fails a submission as today's body does."""

    async def test_an_exception_in_the_builder_fails_the_submission_and_says_so(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        """``failed`` with a short code, the ``failed`` event, the job failed.

        Without the reaction the revision would stay in ``reviewing`` for good:
        nothing takes a submission out of it but the body.
        """
        broken = MagicMock()
        broken.build = AsyncMock(side_effect=RuntimeError("the builder broke"))
        broken.after_delivery = AsyncMock()
        delivered = AsyncMock(return_value=True)

        with (
            _on_the_new_path(delivered),
            patch(
                "course_supporter.homework.path_runner.get_result_builder",
                return_value=broken,
            ),
        ):
            await arq_process_homework(
                _ctx(session_factory, _answers_file(tmp_path)),
                str(seed["job_id"]),
                str(seed["submission_id"]),
            )

        submission, job = await _read_back(session_factory, seed)
        assert (submission.status, submission.error_message) == (
            "failed",
            "path_failed",
        )
        payload = delivered.await_args.kwargs["payload"]
        assert (payload.event, payload.reason) == ("failed", "path_failed")
        assert job.status == "failed", "the seam failed the job on the re-raise"
        broken.after_delivery.assert_not_awaited()

    async def test_a_key_cleared_before_the_work_ran_fails_as_test_not_ready(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        """The doors let it in; the author cleared the key before the work ran."""
        async with session_factory() as session:
            await session.execute(
                TaskReferenceOverride.__table__.delete().where(
                    TaskReferenceOverride.authored_document_id == seed["task_id"]
                )
            )
            await session.commit()

        with _on_the_new_path(AsyncMock(return_value=True)):
            await arq_process_homework(
                _ctx(session_factory, _answers_file(tmp_path)),
                str(seed["job_id"]),
                str(seed["submission_id"]),
            )

        submission, _ = await _read_back(session_factory, seed)
        assert (submission.status, submission.error_message) == (
            "failed",
            "test_not_ready",
        )


class TestAfterDelivery:
    """Task 07, decision 9: the explanations in the student's language are
    asked for after the review is out, once — and another job of the task in
    the way takes nothing back from the student."""

    async def test_another_language_asks_for_its_explanations_once(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        async with session_factory() as session:
            submission = await session.get(HomeworkSubmission, seed["submission_id"])
            assert submission is not None
            submission.response_language = "en"
            await session.commit()
        redis = AsyncMock(enqueue_job=AsyncMock(return_value=None))

        with _on_the_new_path(AsyncMock(return_value=True)):
            await arq_process_homework(
                _ctx(session_factory, _answers_file(tmp_path), redis=redis),
                str(seed["job_id"]),
                str(seed["submission_id"]),
            )

        submission, _ = await _read_back(session_factory, seed)
        assert submission.status == "delivered"
        assert submission.review_result is not None
        assert submission.review_result["language"] == "eng"
        # Shown in the course language, and the review says so.
        assert submission.review_result["test"]["explanations_in_course_language"]
        (english,) = await _versions(session_factory, seed["task_id"], language="eng")
        (asked,) = await _explanation_jobs(session_factory, seed["task_id"])
        assert asked.input_params == {"reference_id": str(english.id)}
        redis.enqueue_job.assert_awaited_once()
        # Asking is not paying: the submission's own job still has no call row,
        # only the funds port's decision, and that one without a price.
        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ExternalServiceCall).where(
                            ExternalServiceCall.job_id == seed["job_id"]
                        )
                    )
                ).scalars()
            )
        assert [(r.action, r.cost_usd) for r in rows] == [(FUNDS_PORT_ACTION, None)]

    async def test_another_job_in_the_way_leaves_the_delivered_review_alone(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        """The text moved and a job of the task is in flight.

        The review reads the key as it will stand and goes out without model
        explanations for the new text; the carrying after delivery is refused
        by the database, rolled back — no version without its job — and logged.
        """
        async with session_factory() as session:
            task = await session.get(AuthoredDocument, seed["task_id"])
            assert task is not None
            task.content_hash = "c2" + "0" * 62
            session.add(
                Job(
                    tenant_id=seed["tenant_id"],
                    job_type="key_explanation",
                    subject_type="authored_document",
                    subject_id=seed["task_id"],
                    input_params={"reference_id": str(uuid.uuid4())},
                    status="queued",
                )
            )
            await session.commit()
        (in_the_way,) = await _explanation_jobs(session_factory, seed["task_id"])
        redis = AsyncMock(enqueue_job=AsyncMock(return_value=None))

        with capture_logs() as logs, _on_the_new_path(AsyncMock(return_value=True)):
            await arq_process_homework(
                _ctx(session_factory, _answers_file(tmp_path), redis=redis),
                str(seed["job_id"]),
                str(seed["submission_id"]),
            )

        submission, job = await _read_back(session_factory, seed)
        assert submission.status == "delivered"
        assert job.status == "complete", "a collision is not the submission's failure"
        assert submission.review_markdown is not None
        assert "Пояснення до цього питання немає." in submission.review_markdown
        assert (
            await _versions(
                session_factory, seed["task_id"], source_content_hash="c2" + "0" * 62
            )
            == []
        ), "no version without its job"
        async with session_factory() as session:
            layer = await session.scalar(
                select(TaskReferenceOverride).where(
                    TaskReferenceOverride.authored_document_id == seed["task_id"]
                )
            )
        assert layer is not None
        assert (layer.source_content_hash, layer.carried_over) == (_TEXT_HASH, False)
        assert [
            job.id for job in await _explanation_jobs(session_factory, seed["task_id"])
        ] == [in_the_way.id]
        assert "test_explanations_request_deferred" in [e["event"] for e in logs]
        redis.enqueue_job.assert_not_awaited()

    async def test_a_follow_up_that_fails_takes_nothing_back(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        """Any failure after delivery is logged; the review and the job stand."""
        failing = AsyncMock(side_effect=RuntimeError("the request broke"))
        redis = AsyncMock(enqueue_job=AsyncMock(return_value=None))

        with (
            capture_logs() as logs,
            _on_the_new_path(AsyncMock(return_value=True)),
            patch(
                "course_supporter.homework.test_result.ReferenceService.request_explanations",
                new=failing,
            ),
        ):
            await arq_process_homework(
                _ctx(session_factory, _answers_file(tmp_path), redis=redis),
                str(seed["job_id"]),
                str(seed["submission_id"]),
            )

        submission, job = await _read_back(session_factory, seed)
        failing.assert_awaited_once()
        assert (submission.status, job.status) == ("delivered", "complete")
        assert "path_after_delivery_failed" in [e["event"] for e in logs]


class TestTodaysMentorIsNotDisturbed:
    async def test_the_new_path_is_not_entered_when_the_switch_is_off(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        """Criterion 3: with the switch off, today's body runs and nothing else.

        Today's body is recognised by the one call only it makes on this route —
        the ``safety_check`` ladder stage through today's own function. The new
        path's body must not be entered, and must leave no trace: no checkpoint
        on the job.

        The execution seam swallows a body exception (it has already written the
        terminal), so the proof is what was called, not what was raised.
        """
        answers = tmp_path / "answers.txt"
        answers.write_text("1. b\n", encoding="utf-8")
        runner = AsyncMock()
        todays_safety = AsyncMock(
            return_value=SafetyResult(
                source="stage2",
                is_safe=False,
                violations=[],
                confidence=0.9,
                reasoning="not this time",
            )
        )

        with (
            patch(
                "course_supporter.homework.path_runner.get_path_config",
                return_value=_config(test_on_new_path=False),
            ),
            patch("course_supporter.homework.path_runner._run_path", new=runner),
            # ONE patch, at the only place the name lives: ``api/tasks`` imports
            # it inside the function body, so there is no module attribute there
            # to patch — a second patch on that path would create an attribute
            # nobody reads and quietly weaken the proof below.
            patch(
                "course_supporter.security.stage2.run_stage2_safety_check",
                new=todays_safety,
            ),
            patch(
                "course_supporter.homework.webhook.deliver_webhook",
                new=AsyncMock(return_value=True),
            ),
        ):
            await arq_process_homework(
                _ctx(session_factory, answers),
                str(seed["job_id"]),
                str(seed["submission_id"]),
            )

        todays_safety.assert_awaited()
        runner.assert_not_awaited()

        async with session_factory() as session:
            job = await session.get(Job, seed["job_id"])
            submission = await session.get(HomeworkSubmission, seed["submission_id"])
        assert job is not None
        # The new path writes a checkpoint the moment it starts; there is none.
        assert job.stage_progress is None
        # And today's body reached its own refusal, as it would have before.
        assert submission is not None
        assert submission.status == "rejected"
