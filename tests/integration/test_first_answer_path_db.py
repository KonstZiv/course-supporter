"""A retried task does not pay for its first answer twice (DD-SP-DG), through the body.

The evaluation stage asks about every item, then once more about the items
whose verdict cannot stand. When that second request gets no answer the body
re-queues the task (``arq.Retry``) and its checkpoint says only "the stage is
not done". Here the real ARQ task, seam, body, evaluation executor with the
shipped stage description, verdict repository and register run twice over a
real Redis; the doubles are the router (the alternative is a paid call), the
criteria source, the funds port and the webhook, as in
``test_text_review_e2e_db.py``, whose world they share.

Lock: across both passes the register holds exactly one row of the first
request, and the verdict rows of the second pass name that row.

Requires ``docker compose up -d``; run with ``--run-db --run-redis``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from arq import Retry
from arq.connections import ArqRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_process_homework
from course_supporter.call_outcome import RequestKind
from course_supporter.homework.path_checkpoint import PathCheckpoint, StageState
from course_supporter.homework.path_config import PathConfig
from course_supporter.homework.path_stages import CRITERIA_EVALUATION
from course_supporter.llm.error_categories import LadderExhaustedError, LadderStop
from course_supporter.llm.ladder_config import StageConfig
from course_supporter.llm.stage_router import StageResult
from course_supporter.service_logging import _persist
from course_supporter.storage.orm import ExternalServiceCall, Job
from tests._helpers.priced import priced_exact
from tests.integration.test_text_review_e2e_db import (
    _ctx,
    _evaluation,
    _ListSource,
    _PortDouble,
    _shipped,
    _submission,
    _submit,
    _verdict,
    _webhook,
    world,  # noqa: F401 - the fixture: a task with its list, and a student
)

pytestmark = [pytest.mark.requires_db, pytest.mark.requires_redis]

# c1's quote is not in the work: the first answer has one item to ask again.
_FIRST = _evaluation(
    _verdict("c1", "met", quote="def solve(m): pass"),
    _verdict("c2.p1", "met", quote="return n * 2"),
    _verdict("c2.p2", "not_met", missing="Немає перевірки від'ємного n."),
    _verdict("c3", "not_met", missing="Немає докстрінга."),
)
_REPEAT = _evaluation(_verdict("c1", "met", quote="def solve(n):"))


class _TheRepeatFailsOnce:
    """The router: the first request is answered; the repeat fails, then answers.

    Each call leaves the register row a real call would, under its request
    kind, and names it as the answering call.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.kinds: list[RequestKind] = []
        self.first_asked_about: list[list[str]] = []
        self._session_factory = session_factory
        self._repeat_fails = True

    async def execute_stage(
        self,
        stage: StageConfig,
        stage_name: str,
        /,
        *,
        response_validator: Callable[[str], None] | None = None,
        repeat_on_rung: int | None = None,
        **render: Any,
    ) -> StageResult:
        assert stage_name == CRITERIA_EVALUATION
        repeat = repeat_on_rung is not None
        kind = RequestKind.ITEM_REPEAT if repeat else RequestKind.FIRST
        self.kinds.append(kind)
        if not repeat:
            self.first_asked_about.append(list(render["items"]))
        rung = stage.ladder[0]
        failing = repeat and self._repeat_fails
        call_id = await _persist(
            self._session_factory,
            action=stage_name,
            strategy="default",
            provider=rung.provider,
            model_id=rung.model,
            unit_type="tokens",
            priced=None if failing else priced_exact(0.02),
            success=not failing,
            request_kind=kind,
        )
        if failing:
            self._repeat_fails = False
            raise LadderExhaustedError(
                stage_name,
                [(rung.provider, rung.model, "a bad minute")],
                stop=LadderStop.EXHAUSTED,
            )
        content = _REPEAT if repeat else _FIRST
        if response_validator is not None:
            response_validator(content)
        return StageResult(
            content=content,
            provider_used=rung.provider,
            model_used=rung.model,
            attempt_count=1,
            call_id=call_id,
        )


def _only_the_evaluation() -> PathConfig:
    """The shipped file, with a task's first-submission path cut to the one stage."""
    data = _shipped().model_dump(mode="json")
    data["task_types"]["task"]["paths"]["first"] = [CRITERIA_EVALUATION]
    return PathConfig.model_validate(data)


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    router: _TheRepeatFailsOnce,
    source: _ListSource,
    arq: ArqRedis,
    tmp_path: Path,
    *,
    submission_id: uuid.UUID,
    job_id: uuid.UUID,
    job_try: int,
) -> None:
    config = _only_the_evaluation()
    ctx = _ctx(session_factory, router, tmp_path, arq=arq)  # type: ignore[arg-type]
    ctx["job_try"] = job_try
    with (
        patch(
            "course_supporter.homework.path_runner.get_path_config",
            return_value=config,
        ),
        patch(
            "course_supporter.homework.path_continuation.get_path_config",
            return_value=config,
        ),
        patch(
            "course_supporter.homework.path_runner.get_result_builder",
            return_value=None,
        ),
        patch(
            "course_supporter.homework.path_runner.build_funds_port",
            return_value=_PortDouble(),
        ),
        patch(
            "course_supporter.homework.criteria_evaluation.build_criteria_list_service",
            return_value=source,
        ),
        patch("course_supporter.homework.webhook.deliver_webhook", new=_webhook()),
    ):
        await arq_process_homework(ctx, str(job_id), str(submission_id))


async def test_a_retried_task_pays_for_its_first_request_once(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],  # noqa: F811
    arq_redis: ArqRedis,
    tmp_path: Path,
) -> None:
    router = _TheRepeatFailsOnce(session_factory)
    source = _ListSource(world["in_force"])
    submission_id, job_id = await _submit(session_factory, world)

    with pytest.raises(Retry):
        await _run(
            session_factory,
            router,
            source,
            arq_redis,
            tmp_path,
            submission_id=submission_id,
            job_id=job_id,
            job_try=1,
        )

    # Pass one: the stage is not done, and its first answer is kept.
    async with session_factory() as session:
        job = await session.get(Job, job_id)
    assert job is not None
    checkpoint = PathCheckpoint.model_validate(job.stage_progress)
    assert checkpoint.stages[CRITERIA_EVALUATION] is StageState.PENDING
    assert len(await arq_redis.keys("cs:first_answer:*")) == 1

    await _run(
        session_factory,
        router,
        source,
        arq_redis,
        tmp_path,
        submission_id=submission_id,
        job_id=job_id,
        job_try=2,
    )

    # Pass two began at the repeat: the first request was asked once in all.
    assert router.kinds == [
        RequestKind.FIRST,
        RequestKind.ITEM_REPEAT,
        RequestKind.ITEM_REPEAT,
    ]
    assert len(router.first_asked_about) == 1
    assert await arq_redis.keys("cs:first_answer:*") == []
    submission = await _submission(session_factory, submission_id)
    assert submission.status == "delivered"
    async with session_factory() as session:
        calls = list(
            (
                await session.execute(
                    select(ExternalServiceCall).where(
                        ExternalServiceCall.job_id == job_id,
                        ExternalServiceCall.action == CRITERIA_EVALUATION,
                    )
                )
            ).scalars()
        )
    firsts = [c for c in calls if c.request_kind == RequestKind.FIRST.value]
    assert len(firsts) == 1
    rows = await _rows_of(session_factory, submission_id)
    assert rows["c2.p1"].answered_by_call_id == firsts[0].id
    assert rows["c3"].answered_by_call_id == firsts[0].id
    assert rows["c1"].retried is True
    assert rows["c1"].answered_by_call_id != firsts[0].id


async def _rows_of(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> dict[str, Any]:
    from course_supporter.storage.orm import SubmissionCriterionVerdict

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(SubmissionCriterionVerdict).where(
                    SubmissionCriterionVerdict.submission_id == submission_id
                )
            )
        ).scalars()
        return {row.item_id: row for row in rows}
