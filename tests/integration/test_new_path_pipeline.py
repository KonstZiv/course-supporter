"""The new path end to end, on the type whose path makes no model call.

Acceptance criterion 4 (mentor-rebuild task 03): a ``test`` submission walks the
new path from the doors to "reviewed" without a single model call, the funds
port's numbers land in the register under the same path key the checkpoint
carries, and the webhook's shape is untouched. Criterion 3 is here too: with the
switch on today's Mentor the new path is not entered at all.

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

from course_supporter.api.tasks import arq_process_homework
from course_supporter.funds_port import FUNDS_PORT_ACTION
from course_supporter.homework.path_checkpoint import PathCheckpoint
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    SubmissionState,
)
from course_supporter.security.schemas import SafetyResult
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)

pytestmark = pytest.mark.requires_db

_WEBHOOK_URL = "https://example.com/hook"


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
        )
        session.add(task)
        await session.flush()
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
            file_url="s3://bucket/homework/answers.txt",
            file_type="text/plain",
            original_filename="answers.txt",
            webhook_url=_WEBHOOK_URL,
            response_language="en",
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
        ids = {"submission_id": submission.id, "job_id": job.id}

    yield ids

    async with session_factory() as session:
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


def _ctx(
    session_factory: async_sessionmaker[AsyncSession], tmp_file: Path
) -> dict[str, Any]:
    s3 = MagicMock()
    s3.extract_key = MagicMock(return_value="homework/answers.txt")
    s3.download_file = AsyncMock(return_value=tmp_file)
    return {
        "session_factory": session_factory,
        "stage_router": MagicMock(),
        "s3_client": s3,
    }


class TestTestTypeEndToEnd:
    async def test_walks_the_path_to_reviewed_without_calling_a_model(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        answers = tmp_path / "answers.txt"
        answers.write_text("1. b\n2. c\n3. a\n", encoding="utf-8")

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
        # No review ran, so nothing was written for one (task 07 brings it).
        assert submission.review_result is None
        assert submission.review_markdown is None
        assert submission.score is None

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
        """Its own defaults fill what no review wrote — a temporary result."""
        answers = tmp_path / "answers.txt"
        answers.write_text("1. b\n", encoding="utf-8")
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
        assert payload.review.score == 0
        assert payload.review.passed is False
        assert payload.review.review_text == ""


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
            patch(
                "course_supporter.api.tasks.run_stage2_safety_check",
                new=todays_safety,
                create=True,
            ),
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
