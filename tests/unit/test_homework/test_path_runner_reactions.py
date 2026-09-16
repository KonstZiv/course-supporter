"""What the body does when a stage produces nothing (mentor-rebuild task 03).

Three endings, three reactions, and the difference is read from the ladder's own
answer. Pure tests on the reaction itself: what is saved, what is written, what
is raised — the machinery around it is exercised end to end elsewhere.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from arq import Retry

from course_supporter.funds_port import SubmissionContext, SubmissionOutcome
from course_supporter.homework.path_checkpoint import (
    FreezeReason,
    PathCheckpoint,
)
from course_supporter.homework.path_config import PathKey, SubmissionState
from course_supporter.homework.path_runner import _stage_produced_nothing
from course_supporter.llm.error_categories import LadderStop
from course_supporter.models.source import AssignmentType

if TYPE_CHECKING:
    from collections.abc import Iterator

_KEY = PathKey(AssignmentType.TASK, SubmissionState.FIRST)
_SUBMISSION = uuid.uuid4()
_JOB = uuid.uuid4()


@pytest.fixture()
def saved(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[PathCheckpoint]]:
    """Capture what the reaction saves, without a database."""
    captured: list[PathCheckpoint] = []

    async def _save(
        _session: object,
        _job_id: uuid.UUID,
        checkpoint: PathCheckpoint,
        *,
        current_stage: str | None,
    ) -> None:
        captured.append(checkpoint)

    monkeypatch.setattr("course_supporter.homework.path_runner.save_checkpoint", _save)
    yield captured


def _context() -> SubmissionContext:
    return SubmissionContext(
        tenant_id=uuid.uuid4(),
        student_id=uuid.uuid4(),
        submission_id=_SUBMISSION,
        path_key=_KEY,
    )


async def _react(
    stop: LadderStop,
    *,
    retries: int = 0,
    job_try: int = 1,
    hw_repo: AsyncMock | None = None,
    port: AsyncMock | None = None,
) -> None:
    session = AsyncMock()
    checkpoint = PathCheckpoint.started(_KEY, ["safety", "verdicts"]).with_stage_done(
        "safety"
    )
    for _ in range(retries):
        checkpoint = checkpoint.retried()
    await _stage_produced_nothing(
        session,
        hw_repo or AsyncMock(),
        port or AsyncMock(),
        _context(),
        submission_id=_SUBMISSION,
        job_id=_JOB,
        checkpoint=checkpoint,
        stage_name="verdicts",
        stop=stop,
        job_try=job_try,
        log=AsyncMock(),
    )


class TestTheTwoCeilingsAreHeld:
    @pytest.mark.parametrize(
        ("stop", "reason"),
        [
            (LadderStop.MONEY_CEILING, FreezeReason.STAGE_MONEY_CEILING),
            (LadderStop.OUTPUT_CEILING, FreezeReason.OUTPUT_CEILING),
        ],
    )
    async def test_the_run_is_frozen_with_its_own_reason(
        self, saved: list[PathCheckpoint], stop: LadderStop, reason: FreezeReason
    ) -> None:
        await _react(stop)

        (checkpoint,) = saved
        assert checkpoint.frozen_stage == "verdicts"
        assert checkpoint.frozen_reason is reason
        assert checkpoint.retries == 0

    async def test_the_submission_is_not_touched_and_the_port_is_not_told(
        self,
        saved: list[PathCheckpoint],
    ) -> None:
        """A held revision has not ended — the student still reads "in work"."""
        hw_repo, port = AsyncMock(), AsyncMock()

        await _react(LadderStop.MONEY_CEILING, hw_repo=hw_repo, port=port)

        hw_repo.update_status.assert_not_awaited()
        port.release_remainder.assert_not_awaited()

    async def test_a_ceiling_is_never_retried(
        self, saved: list[PathCheckpoint]
    ) -> None:
        """The next attempt would meet the same limit with the same input."""
        await _react(LadderStop.OUTPUT_CEILING)

        assert saved[0].retries == 0


class TestAnOrdinaryExhaustionIsRetried:
    async def test_the_body_requeues_itself_and_counts_the_try(
        self, saved: list[PathCheckpoint]
    ) -> None:
        with pytest.raises(Retry):
            await _react(LadderStop.EXHAUSTED)

        (checkpoint,) = saved
        assert checkpoint.retries == 1
        assert checkpoint.frozen_reason is FreezeReason.PROVIDER_UNAVAILABLE

    async def test_the_count_lives_in_the_checkpoint_not_in_the_queue(
        self, saved: list[PathCheckpoint]
    ) -> None:
        with pytest.raises(Retry):
            await _react(LadderStop.EXHAUSTED, retries=1)

        assert saved[0].retries == 2

    async def test_a_spent_budget_ends_the_submission(
        self, saved: list[PathCheckpoint]
    ) -> None:
        """Criterion 5's last clause: failed, its reason code, the port told."""
        from course_supporter.config import get_settings

        hw_repo, port = AsyncMock(), AsyncMock()
        limit = get_settings().submission_path_max_retries

        await _react(LadderStop.EXHAUSTED, retries=limit, hw_repo=hw_repo, port=port)

        hw_repo.update_status.assert_awaited_once()
        assert hw_repo.update_status.await_args.args[1] == "failed"
        assert (
            hw_repo.update_status.await_args.kwargs["error_message"]
            == FreezeReason.PROVIDER_UNAVAILABLE.value
        )
        port.release_remainder.assert_awaited_once()
        assert port.release_remainder.await_args.args[1] is SubmissionOutcome.FAILED

    async def test_the_limit_is_a_setting(self) -> None:
        from course_supporter.config import get_settings

        settings = get_settings()

        assert settings.submission_path_max_retries >= 1
        assert settings.submission_path_retry_defer_s >= 1


class TestTheQueuesBudgetIsWatchedToo:
    """The body must never be the reason the queue runs out of attempts.

    On the final queue attempt the execution seam turns a re-queue into a
    terminal ``failed`` on the JOB and re-raises. A revision left ``received``
    behind a failed job is reachable by none of the three continuations — the
    orphan sweep sees only jobs in flight, the frozen-revision pass only the two
    ceilings — so it would wait for a review that nobody will ever run.
    """

    async def test_the_last_queue_attempt_ends_the_submission_instead(
        self, saved: list[PathCheckpoint]
    ) -> None:
        from course_supporter.config import get_settings

        settings = get_settings()
        hw_repo, port = AsyncMock(), AsyncMock()

        # Its OWN budget is untouched — this is the queue's, running out first.
        await _react(
            LadderStop.EXHAUSTED,
            retries=0,
            job_try=settings.worker_max_tries,
            hw_repo=hw_repo,
            port=port,
        )

        assert hw_repo.update_status.await_args.args[1] == "failed"
        assert (
            hw_repo.update_status.await_args.kwargs["error_message"]
            == FreezeReason.PROVIDER_UNAVAILABLE.value
        )
        port.release_remainder.assert_awaited_once()
        assert port.release_remainder.await_args.args[1] is SubmissionOutcome.FAILED

    async def test_an_earlier_queue_attempt_still_retries(
        self, saved: list[PathCheckpoint]
    ) -> None:
        from course_supporter.config import get_settings

        with pytest.raises(Retry):
            await _react(
                LadderStop.EXHAUSTED,
                job_try=get_settings().worker_max_tries - 1,
            )

    async def test_the_first_queue_attempt_is_one_and_the_last_is_max_tries(
        self, saved: list[PathCheckpoint]
    ) -> None:
        """The numbering the comparison rests on, pinned rather than assumed.

        ARQ increments a Redis counter per attempt, so the FIRST attempt reports
        ``job_try == 1`` (``INCR`` on a missing key returns 1), and it gives up
        when ``job_try > max_tries`` — which makes ``job_try == max_tries`` the
        last attempt that actually runs. The execution seam compares the two the
        same way. Both ends of that range are asserted here, so a change to the
        numbering breaks a test rather than a student's submission.
        """
        from course_supporter.config import get_settings

        last = get_settings().worker_max_tries

        # The first attempt: there is more budget, so the body asks for it.
        with pytest.raises(Retry):
            await _react(LadderStop.EXHAUSTED, job_try=1)

        # The last attempt that will run: the body must end it here itself.
        hw_repo = AsyncMock()
        await _react(LadderStop.EXHAUSTED, job_try=last, hw_repo=hw_repo)
        assert hw_repo.update_status.await_args.args[1] == "failed"

    async def test_a_limit_above_the_queues_cannot_strand_a_revision(
        self,
        saved: list[PathCheckpoint],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The misconfiguration the guard exists for, played out.

        With a path limit at or above ``worker_max_tries`` the body would keep
        asking for one more attempt until the seam refused — and a comparison of
        the two settings at startup would not catch every case anyway, because
        the queue's attempts are also spent by things the path does not control
        (the seam's own missing-job policy). Watching the attempt itself does.
        """
        from course_supporter.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(
            settings, "submission_path_max_retries", settings.worker_max_tries + 5
        )
        hw_repo, port = AsyncMock(), AsyncMock()

        await _react(
            LadderStop.EXHAUSTED,
            retries=0,
            job_try=settings.worker_max_tries,
            hw_repo=hw_repo,
            port=port,
        )

        hw_repo.update_status.assert_awaited_once()
        assert hw_repo.update_status.await_args.args[1] == "failed"
