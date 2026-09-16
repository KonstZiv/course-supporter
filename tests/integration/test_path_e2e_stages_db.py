"""The new path walked end to end WITH stages (mentor-rebuild task 03).

Acceptance criteria 5 and 7, which the task's own suite proved only in pieces:
the only end-to-end walk it shipped is the ``test`` type, whose path lists no
stage at all. Here the type is ``task`` with two stages on its first-submission
path, and everything below the model layer is the real thing — the ARQ task,
the execution seam, the body, the shipped stage executors, the checkpoint on
real ``jobs`` columns, the register.

Two doubles, and only two:

* the ROUTER, because the alternative is a paid call. It counts what it was
  asked to run, answers per scenario, and writes the register row a real call
  would have written — so the price the body reads back is a real sum over
  real rows.
* the FUNDS PORT, because the only shipped implementation always allows and
  criterion 7 is about a refusal.

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
from course_supporter.funds_port import (
    FUNDS_PORT_ACTION,
    FundsAnswer,
    FundsRefusalReason,
    SubmissionContext,
    SubmissionOutcome,
)
from course_supporter.homework.path_checkpoint import (
    FreezeReason,
    PathCheckpoint,
    StageState,
)
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    SubmissionState,
)
from course_supporter.llm.error_categories import LadderExhaustedError, LadderStop
from course_supporter.llm.stage_router import StageResult
from course_supporter.service_logging import _persist
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
_STAGES = ["safety", "attempt_classifier"]

# What each stage's double "costs". Different on purpose: the body reads the
# price back per stage, so equal numbers would not show which row it summed.
_PRICE = {"safety": 0.004, "attempt_classifier": 0.006}

# Valid verdicts for the two shipped executors, so the real parsing runs.
_CONTENT = {
    "safety": (
        '{"source": "stage2", "is_safe": true, "violations": [], '
        '"confidence": 0.99, "reasoning": "nothing of the sort"}'
    ),
    "attempt_classifier": (
        '{"verdict": "match", "confidence": 0.9, "reason": "an honest attempt"}'
    ),
}

# Over the gate's confidence floor (config/sanity.yaml: 0.85), so this one ends
# the path instead of carrying on.
_OFF_TASK = '{"verdict": "mismatch", "confidence": 0.95, "reason": "another task"}'


class _RouterDouble:
    """Stands in for the model layer: counts, answers, and leaves a register row.

    ``script`` maps a stage name to a callable that raises — the scenario's way
    of saying "this stage produced nothing, for this reason". ``once`` empties
    the script entry after it fires, which is how a retry is made to succeed.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        script: dict[str, Any] | None = None,
        once: bool = False,
        watch: uuid.UUID | None = None,
        content: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        # What the job's own columns said at the moment each stage was asked to
        # run — the only place from which "the checkpoint after each stage" is
        # observable while the body is still inside the run.
        self.seen: list[tuple[str, dict[str, Any] | None, str | None]] = []
        self._session_factory = session_factory
        self._script = dict(script or {})
        self._once = once
        self._watch = watch
        self._content = {**_CONTENT, **(content or {})}

    def count(self, stage_name: str) -> int:
        return self.calls.count(stage_name)

    async def execute_stage(
        self,
        stage: Any,
        stage_name: str,
        /,
        *,
        response_validator: Any = None,
        contents: Any = None,
        expects_json: bool = False,
        stop_on_output_ceiling: bool = False,
        money_ceiling_usd: float | None = None,
        **render_context: Any,
    ) -> StageResult:
        self.calls.append(stage_name)
        if self._watch is not None:
            async with self._session_factory() as session:
                job = await session.get(Job, self._watch)
                progress = job.stage_progress if job is not None else None
                self.seen.append(
                    (
                        stage_name,
                        progress if isinstance(progress, dict) else None,
                        job.current_stage if job is not None else None,
                    )
                )
        raiser = self._script.get(stage_name)
        if raiser is not None:
            if self._once:
                self._script.pop(stage_name)
            raise raiser()
        # A real call leaves a priced row under the stage's own name; the body
        # sums exactly these rows to tell the port what the stage cost.
        await _persist(
            self._session_factory,
            action=stage_name,
            strategy="default",
            provider="double",
            model_id="m",
            unit_type="tokens",
            cost_usd=_PRICE[stage_name],
        )
        content = self._content[stage_name]
        if response_validator is not None:
            response_validator(content)
        return StageResult(
            content=content, provider_used="double", model_used="m", attempt_count=1
        )


class _PortDouble:
    """Records the three operations with their arguments; may refuse the first."""

    def __init__(self, *, refuse_first_n: int = 0) -> None:
        self.reserved: list[tuple[SubmissionContext, float]] = []
        self.accounted: list[tuple[SubmissionContext, float]] = []
        self.released: list[tuple[SubmissionContext, SubmissionOutcome]] = []
        self._refusals_left = refuse_first_n

    async def check_and_reserve(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        self.reserved.append((context, ceiling_estimate_usd))
        if self._refusals_left > 0:
            self._refusals_left -= 1
            return FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)
        return FundsAnswer.allowed()

    async def account_stage_cost(
        self, context: SubmissionContext, stage_cost_usd: float
    ) -> None:
        self.accounted.append((context, stage_cost_usd))

    async def release_remainder(
        self, context: SubmissionContext, outcome: SubmissionOutcome
    ) -> None:
        self.released.append((context, outcome))


def _stage(money_usd: float = 0.05) -> dict[str, Any]:
    return {
        "deterministic": False,
        "prompt_ref": "prompts/safety_check/v1.md",
        "requires": [],
        "input_budget_ratio": None,
        "record_output": False,
        "ceilings": {"tool_steps": 0, "money_usd": money_usd, "output_tokens": 8192},
        # Two rungs, because "a trace per rung" is only a claim on a ladder
        # that has more than one.
        "ladder": [
            {
                "provider": "mistral",
                "model": "m",
                "reasoning": None,
                "max_output_tokens": None,
            },
            {
                "provider": "gemini",
                "model": "g",
                "reasoning": None,
                "max_output_tokens": None,
            },
        ],
    }


def _config(*, stages: list[str] | None = None, money_usd: float = 0.05) -> PathConfig:
    """``task`` on the new path, its first-submission path listing two stages."""
    listed = _STAGES if stages is None else stages
    return PathConfig.model_validate(
        {
            "stages": {name: _stage(money_usd) for name in _STAGES},
            "task_types": {
                "task": {
                    "served_by": ServedBy.NEW_PATH.value,
                    "paths": {
                        SubmissionState.FIRST.value: listed,
                        SubmissionState.REPEAT_WITHOUT_REPLIES.value: listed,
                        SubmissionState.REPEAT_WITH_REPLIES.value: listed,
                    },
                },
                "test": {"served_by": "todays_mentor", "paths": {}},
                "short_task": {"served_by": "todays_mentor", "paths": {}},
                "project": {"served_by": "todays_mentor", "paths": {}},
            },
        }
    )


@pytest.fixture()
async def seed(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """One ``task`` submission of one student, with its first job queued."""
    async with session_factory() as session:
        tenant = Tenant(name=f"e2e-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = CourseNode(
            tenant_id=tenant.id, title="Course", order=0, default_language="ukr"
        )
        session.add(node)
        await session.flush()
        doc = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="https://example.com/t",
            task_type="task",
            language="ukr",
        )
        session.add(doc)
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
            authored_document_id=doc.id,
            file_url="s3://bucket/homework/answers.py",
            file_type="text/plain",
            original_filename="answers.py",
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
        ids = {
            "tenant_id": tenant.id,
            "student_id": student.id,
            "submission_id": submission.id,
            "job_id": job.id,
        }

    yield ids

    async with session_factory() as session:
        job_ids = list(
            (
                await session.execute(
                    select(Job.id).where(Job.subject_id == ids["submission_id"])
                )
            ).scalars()
        )
        await session.execute(
            ExternalServiceCall.__table__.delete().where(
                ExternalServiceCall.job_id.in_(job_ids)
            )
        )
        await session.execute(
            HomeworkSubmission.__table__.delete().where(
                HomeworkSubmission.id == ids["submission_id"]
            )
        )
        await session.execute(
            Job.__table__.delete().where(Job.subject_id == ids["submission_id"])
        )
        await session.commit()


_SOURCE = "def solve(n):\n    return n * 2\n"


def _ctx(
    session_factory: async_sessionmaker[AsyncSession],
    router: Any,
    answers: Path,
    *,
    job_try: int = 1,
) -> dict[str, Any]:
    def _download(_key: str) -> Path:
        # The body owns the temp file's lifetime and deletes it on the way out,
        # so a continuation is handed its own download, exactly as a second
        # worker run would be.
        answers.write_text(_SOURCE, encoding="utf-8")
        return answers

    s3 = MagicMock()
    s3.extract_key = MagicMock(return_value="homework/answers.py")
    s3.download_file = AsyncMock(side_effect=_download)
    return {
        "session_factory": session_factory,
        "stage_router": router,
        "s3_client": s3,
        "job_try": job_try,
    }


def _answers(tmp_path: Path) -> Path:
    path = tmp_path / "answers.py"
    path.write_text(_SOURCE, encoding="utf-8")
    return path


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    router: Any,
    port: _PortDouble,
    seed: dict[str, uuid.UUID],
    answers: Path,
    *,
    job_id: uuid.UUID | None = None,
    job_try: int = 1,
    config: PathConfig | None = None,
) -> None:
    """Run the ARQ task through the seam, with the two doubles wired in.

    Patched: the path configuration (the switch lives in a file nobody edits in
    a test), the port constructor (the ARQ task takes no port argument — this
    is the one wiring point the body offers), and the webhook.
    """
    with (
        patch(
            "course_supporter.homework.path_runner.get_path_config",
            return_value=config or _config(),
        ),
        patch(
            "course_supporter.homework.path_runner.AlwaysEnoughFundsPort",
            return_value=port,
        ),
        patch(
            "course_supporter.homework.webhook.deliver_webhook",
            new=AsyncMock(return_value=True),
        ),
    ):
        await arq_process_homework(
            _ctx(session_factory, router, answers, job_try=job_try),
            str(job_id or seed["job_id"]),
            str(seed["submission_id"]),
        )


async def _job(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> Job:
    async with session_factory() as session:
        job = await session.get(Job, job_id)
    assert job is not None
    return job


async def _checkpoint(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> PathCheckpoint:
    job = await _job(session_factory, job_id)
    assert isinstance(job.stage_progress, dict)
    return PathCheckpoint.from_jsonb(job.stage_progress)


async def _submission(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> HomeworkSubmission:
    async with session_factory() as session:
        submission = await session.get(HomeworkSubmission, submission_id)
    assert submission is not None
    return submission


class TestTwoStagesWalkTheWholePath:
    """Criterion 5, the part no shipped test reaches: a path that HAS stages."""

    async def test_each_stage_leaves_a_checkpoint_and_a_price_behind_it(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        router = _RouterDouble(session_factory, watch=seed["job_id"])
        port = _PortDouble()

        await _run(session_factory, router, port, seed, _answers(tmp_path))

        assert router.calls == _STAGES

        # ── The checkpoint after each stage, read from the job's own columns ──
        # When the SECOND stage was asked to run, the database already said the
        # first was done and named it as the current stage. That is the whole
        # point of a checkpoint: it is durable before the next stage starts.
        first_seen, second_seen = router.seen
        assert first_seen[0] == "safety"
        assert PathCheckpoint.from_jsonb(first_seen[1] or {}).stages == {
            "safety": StageState.PENDING,
            "attempt_classifier": StageState.PENDING,
        }
        assert first_seen[2] is None
        assert second_seen[0] == "attempt_classifier"
        assert PathCheckpoint.from_jsonb(second_seen[1] or {}).stages == {
            "safety": StageState.DONE,
            "attempt_classifier": StageState.PENDING,
        }
        assert second_seen[2] == "safety"

        checkpoint = await _checkpoint(session_factory, seed["job_id"])
        assert checkpoint.stages == {
            "safety": StageState.DONE,
            "attempt_classifier": StageState.DONE,
        }
        assert checkpoint.frozen_reason is None
        assert str(checkpoint.path_key) == "task/first"

        # ── The port's second operation after each stage, with the register's
        # own number: one row per stage, priced, summed by stage name ──
        assert [cost for _, cost in port.accounted] == [
            pytest.approx(_PRICE["safety"]),
            pytest.approx(_PRICE["attempt_classifier"]),
        ]
        assert [context.path_key for context, _ in port.accounted] == [
            checkpoint.path_key,
            checkpoint.path_key,
        ]
        assert len(port.reserved) == 1
        assert port.reserved[0][1] == pytest.approx(0.10)  # two stages at 0.05
        assert [outcome for _, outcome in port.released] == [
            SubmissionOutcome.COMPLETED
        ]

        submission = await _submission(session_factory, seed["submission_id"])
        assert submission.status == "delivered"
        job = await _job(session_factory, seed["job_id"])
        assert job.status == "complete"

    async def test_the_prices_come_from_the_register_not_from_the_config(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        """The numbers the port heard are the sums of real rows, per stage."""
        router = _RouterDouble(session_factory)
        port = _PortDouble()

        await _run(session_factory, router, port, seed, _answers(tmp_path))

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
        priced = {r.action: r.cost_usd for r in rows if r.action != FUNDS_PORT_ACTION}
        assert priced == {
            "safety": pytest.approx(_PRICE["safety"]),
            "attempt_classifier": pytest.approx(_PRICE["attempt_classifier"]),
        }
        assert [cost for _, cost in port.accounted] == [
            pytest.approx(priced["safety"]),
            pytest.approx(priced["attempt_classifier"]),
        ]


class TestARetryReplaysOnlyTheStageThatFailed:
    """Criterion 5: the body re-queues itself, and the first stage is not redone."""

    async def test_the_second_attempt_starts_at_the_stage_that_produced_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        from arq import Retry

        answers = _answers(tmp_path)
        router = _RouterDouble(
            session_factory,
            script={
                "attempt_classifier": lambda: LadderExhaustedError(
                    "attempt_classifier",
                    [("mistral", "m", "every rung had a bad minute")],
                    stop=LadderStop.EXHAUSTED,
                )
            },
            once=True,
        )
        port = _PortDouble()

        with pytest.raises(Retry):
            await _run(session_factory, router, port, seed, answers, job_try=1)

        frozen = await _checkpoint(session_factory, seed["job_id"])
        assert frozen.stages["safety"] is StageState.DONE
        assert frozen.stages["attempt_classifier"] is StageState.PENDING
        assert frozen.frozen_reason is FreezeReason.PROVIDER_UNAVAILABLE
        assert frozen.retries == 1
        # Nothing ended: the port was told about the one stage that finished,
        # and never about the submission.
        assert [cost for _, cost in port.accounted] == [pytest.approx(_PRICE["safety"])]
        assert port.released == []

        await _run(session_factory, router, port, seed, answers, job_try=2)

        # The whole run asked for the first stage exactly once.
        assert router.count("safety") == 1
        assert router.count("attempt_classifier") == 2
        done = await _checkpoint(session_factory, seed["job_id"])
        assert done.stages == {
            "safety": StageState.DONE,
            "attempt_classifier": StageState.DONE,
        }
        submission = await _submission(session_factory, seed["submission_id"])
        assert submission.status == "delivered"


class TestAnOrphanContinuesFromItsCheckpoint:
    """Criterion 5: the sweep re-queues it, and only the unfinished stage runs."""

    async def test_the_continuation_runs_the_second_stage_only(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        from arq import Retry

        from course_supporter.homework.path_continuation import (
            resume_orphaned_path_job,
        )

        answers = _answers(tmp_path)
        # Stop after the first stage the only way a run stops there: the second
        # produced nothing. The job is then an orphan carrying a checkpoint.
        router = _RouterDouble(
            session_factory,
            script={
                "attempt_classifier": lambda: LadderExhaustedError(
                    "attempt_classifier",
                    [("mistral", "m", "bad minute")],
                    stop=LadderStop.EXHAUSTED,
                )
            },
            once=True,
        )
        port = _PortDouble()
        with pytest.raises(Retry):
            await _run(session_factory, router, port, seed, answers, job_try=1)

        arq = MagicMock()
        arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-1"))
        async with session_factory() as session:
            job = await session.get(Job, seed["job_id"])
            assert job is not None
            requeued = await resume_orphaned_path_job(job, arq, session)
        assert requeued is True
        arq.enqueue_job.assert_awaited_once()

        # The continuation runs in the SAME job the sweep handed back to ARQ.
        await _run(session_factory, router, port, seed, answers, job_try=1)

        assert router.count("safety") == 1
        assert router.count("attempt_classifier") == 2
        done = await _checkpoint(session_factory, seed["job_id"])
        assert done.stages == {
            "safety": StageState.DONE,
            "attempt_classifier": StageState.DONE,
        }
        submission = await _submission(session_factory, seed["submission_id"])
        assert submission.status == "delivered"


class TestASpentRetryBudgetEndsTheSubmission:
    """Criterion 5's last clause, end to end rather than on the reaction alone."""

    async def test_failed_with_its_reason_code_and_the_port_told(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        from arq import Retry

        from course_supporter.api.routes._portal_shared import curated_presentation

        answers = _answers(tmp_path)
        router = _RouterDouble(
            session_factory,
            script={
                "attempt_classifier": lambda: LadderExhaustedError(
                    "attempt_classifier",
                    [("mistral", "m", "every rung, every time")],
                    stop=LadderStop.EXHAUSTED,
                )
            },
        )
        port = _PortDouble()

        # Two settings bound the run: the path's own limit (2) and the queue's
        # (3). The third attempt is the last of both.
        for attempt in (1, 2):
            with pytest.raises(Retry):
                await _run(
                    session_factory, router, port, seed, answers, job_try=attempt
                )
        await _run(session_factory, router, port, seed, answers, job_try=3)

        submission = await _submission(session_factory, seed["submission_id"])
        assert submission.status == "failed"
        assert submission.error_message == FreezeReason.PROVIDER_UNAVAILABLE.value
        presentation = curated_presentation(submission)
        assert presentation.state == "not_opened"
        assert presentation.reason_code == "processing_failed"

        assert [outcome for _, outcome in port.released] == [SubmissionOutcome.FAILED]
        assert router.count("safety") == 1
        assert router.count("attempt_classifier") == 3


class TestARefusedPortHoldsTheRevision:
    """Criterion 7: the only shipped port always allows, so this one refuses."""

    async def test_held_before_a_single_call_and_the_job_ends_normally(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        router = _RouterDouble(session_factory)
        port = _PortDouble(refuse_first_n=1)

        await _run(session_factory, router, port, seed, _answers(tmp_path))

        # Not one stage was asked to run: the refusal comes before anything is
        # paid for, which is the whole point of asking first.
        assert router.calls == []
        assert port.accounted == []
        assert port.released == []
        assert len(port.reserved) == 1

        submission = await _submission(session_factory, seed["submission_id"])
        assert submission.status == "awaiting_funds"
        checkpoint = await _checkpoint(session_factory, seed["job_id"])
        assert checkpoint.frozen_reason is FreezeReason.AWAITING_FUNDS
        assert checkpoint.frozen_stage == "safety"
        assert checkpoint.stages == {
            "safety": StageState.PENDING,
            "attempt_classifier": StageState.PENDING,
        }

        # The job in the queue finished — a hold ends its job, because the
        # database allows a revision only one in flight at a time.
        job = await _job(session_factory, seed["job_id"])
        assert job.status == "complete"

    async def test_a_top_up_starts_a_new_job_that_asks_again_and_starts_over(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        from course_supporter.homework.path_continuation import resume_after_top_up

        answers = _answers(tmp_path)
        router = _RouterDouble(session_factory)
        port = _PortDouble(refuse_first_n=1)
        await _run(session_factory, router, port, seed, answers)
        assert router.calls == []

        arq = MagicMock()
        arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-топ"))
        async with session_factory() as session:
            started = await resume_after_top_up(
                arq=arq, session=session, submission_id=seed["submission_id"]
            )
        assert started is True

        async with session_factory() as session:
            jobs = list(
                (
                    await session.execute(
                        select(Job)
                        .where(Job.subject_id == seed["submission_id"])
                        .order_by(Job.queued_at.asc())
                    )
                ).scalars()
            )
        assert len(jobs) == 2
        continuation = jobs[-1]
        assert continuation.id != seed["job_id"]

        await _run(session_factory, router, port, seed, answers, job_id=continuation.id)

        # The first operation of the port ran again on the new job, and the
        # stages started from the beginning: nothing was done before the hold.
        assert len(port.reserved) == 2
        assert router.calls == _STAGES
        checkpoint = await _checkpoint(session_factory, continuation.id)
        assert checkpoint.stages == {
            "safety": StageState.DONE,
            "attempt_classifier": StageState.DONE,
        }
        submission = await _submission(session_factory, seed["submission_id"])
        assert submission.status == "delivered"

    async def test_a_top_up_that_ends_the_path_early_is_allowed_to_end_it(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        """The other way a continuation writes a status: a stage ends the path.

        `awaiting_funds -> mismatch` is as illegal as `-> reviewing`, so the
        hold has to be gone by the time a stage answers, not only by the time
        the path finishes.
        """
        from course_supporter.api.routes._portal_shared import curated_presentation
        from course_supporter.homework.path_continuation import resume_after_top_up

        answers = _answers(tmp_path)
        router = _RouterDouble(
            session_factory, content={"attempt_classifier": _OFF_TASK}
        )
        port = _PortDouble(refuse_first_n=1)
        await _run(session_factory, router, port, seed, answers)
        assert router.calls == []

        arq = MagicMock()
        arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-2"))
        async with session_factory() as session:
            assert await resume_after_top_up(
                arq=arq, session=session, submission_id=seed["submission_id"]
            )
        async with session_factory() as session:
            continuation = list(
                (
                    await session.execute(
                        select(Job)
                        .where(Job.subject_id == seed["submission_id"])
                        .order_by(Job.queued_at.asc())
                    )
                ).scalars()
            )[-1]

        await _run(session_factory, router, port, seed, answers, job_id=continuation.id)

        assert router.calls == _STAGES
        submission = await _submission(session_factory, seed["submission_id"])
        assert submission.status == "mismatch"
        presentation = curated_presentation(submission)
        assert presentation.state == "not_an_attempt"
        assert presentation.reason_code == "mismatch"
        # The stage that answered ends the submission, so the port hears that
        # it ended — completed, not failed: the path did its work.
        assert [outcome for _, outcome in port.released] == [
            SubmissionOutcome.COMPLETED
        ]


def _priced_registry(per_1k: float) -> Any:
    """A registry that prices both rungs, so a ceiling can be below an attempt."""
    from course_supporter.llm.registry import ModelRegistryConfig

    def _model(model_id: str) -> dict[str, Any]:
        return {
            "id": model_id,
            "cost_per_1k_in": per_1k,
            "cost_per_1k_out": per_1k,
            "max_output_tokens": 1000,
            "max_context": 100_000,
        }

    return ModelRegistryConfig.model_validate(
        {
            "providers": {
                "mistral": {"type": "llm", "models": [_model("m")]},
                "gemini": {"type": "llm", "models": [_model("g")]},
            },
            "actions": {},
        }
    )


def _real_router(
    session_factory: async_sessionmaker[AsyncSession], provider: Any
) -> Any:
    """The shipped router, with the model layer doubled one level lower.

    Criterion 9 asks for a trace per rung in the register, and only the real
    router writes one — so here the double is the PROVIDER, not the router.
    """
    from course_supporter.llm.ladder_config import LadderConfig
    from course_supporter.llm.stage_router import StageRouter

    return StageRouter(
        LadderConfig(stages={}),
        {"mistral": provider, "gemini": provider},
        registry=_priced_registry(1.0),
        session_factory=session_factory,
    )


def _provider_double(content: str) -> Any:
    """A provider that answers if it is ever called — and must not be."""
    from course_supporter.llm.error_categories import ErrorCategory
    from course_supporter.llm.providers.base import LLMProvider
    from course_supporter.llm.schemas import LLMResponse

    p = AsyncMock(spec=LLMProvider)
    p.enabled = True
    p.complete = AsyncMock(
        return_value=LLMResponse(
            content=content,
            provider="mistral",
            model_id="m",
            tokens_in=10,
            tokens_out=20,
            latency_ms=42,
            cost_usd=0.001,
        )
    )
    p.classify_error = lambda _exc: ErrorCategory.SEMANTIC
    return p


class TestTheMoneyCeilingSkipsBeforeItSpends:
    """Criterion 9, through the body rather than on the router alone."""

    async def test_no_rung_is_called_and_the_revision_is_frozen(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        from course_supporter.api.routes._portal_shared import curated_presentation
        from course_supporter.call_outcome import CallOutcome, SkipReason

        provider = _provider_double(_CONTENT["safety"])
        port = _PortDouble()

        await _run(
            session_factory,
            _real_router(session_factory, provider),
            port,
            seed,
            _answers(tmp_path),
            config=_config(stages=["safety"], money_usd=0.000_001),
        )

        provider.complete.assert_not_awaited()

        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ExternalServiceCall)
                        .where(
                            ExternalServiceCall.job_id == seed["job_id"],
                            ExternalServiceCall.action == "safety",
                        )
                        .order_by(ExternalServiceCall.created_at.asc())
                    )
                ).scalars()
            )
        # One trace per rung of the ladder, each naming the money ceiling.
        assert [r.outcome for r in rows] == [CallOutcome.SKIPPED, CallOutcome.SKIPPED]
        assert [r.skip_reason for r in rows] == [
            SkipReason.MONEY_CEILING_EXCEEDED,
            SkipReason.MONEY_CEILING_EXCEEDED,
        ]
        assert [r.cost_usd for r in rows] == [None, None]

        checkpoint = await _checkpoint(session_factory, seed["job_id"])
        assert checkpoint.frozen_reason is FreezeReason.STAGE_MONEY_CEILING
        assert checkpoint.frozen_stage == "safety"
        assert checkpoint.stages == {"safety": StageState.PENDING}
        assert checkpoint.retries == 0

        # A ceiling is not a retry and not an ending: the submission is still
        # being checked, and the port was never told it ended.
        submission = await _submission(session_factory, seed["submission_id"])
        assert submission.status == "received"
        assert curated_presentation(submission).state == "in_progress"
        assert port.released == []
        assert port.accounted == []

    async def test_raising_the_ceiling_lets_the_startup_pass_run_the_stage(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed: dict[str, uuid.UUID],
        tmp_path: Path,
    ) -> None:
        from course_supporter.homework.path_continuation import (
            sweep_frozen_revisions,
        )

        answers = _answers(tmp_path)
        provider = _provider_double(_CONTENT["safety"])
        port = _PortDouble()
        await _run(
            session_factory,
            _real_router(session_factory, provider),
            port,
            seed,
            answers,
            config=_config(stages=["safety"], money_usd=0.000_001),
        )
        provider.complete.assert_not_awaited()

        arq = MagicMock()
        arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-sweep"))
        await sweep_frozen_revisions(session_factory, arq)

        async with session_factory() as session:
            jobs = list(
                (
                    await session.execute(
                        select(Job)
                        .where(Job.subject_id == seed["submission_id"])
                        .order_by(Job.queued_at.asc())
                    )
                ).scalars()
            )
        assert len(jobs) == 2, "the pass makes a NEW job; a hold ended the old one"
        continuation = jobs[-1]

        # The edit that lifts the hold is the ceiling in the configuration.
        await _run(
            session_factory,
            _real_router(session_factory, provider),
            port,
            seed,
            answers,
            job_id=continuation.id,
            config=_config(stages=["safety"], money_usd=10.0),
        )

        provider.complete.assert_awaited_once()
        checkpoint = await _checkpoint(session_factory, continuation.id)
        assert checkpoint.stages == {"safety": StageState.DONE}
        assert checkpoint.frozen_reason is None
        submission = await _submission(session_factory, seed["submission_id"])
        assert submission.status == "delivered"
