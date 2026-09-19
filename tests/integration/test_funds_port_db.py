"""Acceptance 3 and 4 (mentor-rebuild task 02): the funds port on a real register.

Nothing in this task calls the port, so each test is the caller: it reads a
path file the way a process does at startup, computes the path's estimate,
asks the port, and reads the row the answer left in the call register.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.call_outcome import FundsDecision
from course_supporter.funds_port import (
    FUNDS_PORT_ACTION,
    AlwaysEnoughFundsPort,
    FundsAnswer,
    FundsPort,
    FundsRefusalReason,
    SubmissionContext,
    SubmissionOutcome,
    VersionWorkContext,
    VersionWorkKind,
)
from course_supporter.homework.path_config import (
    PathConfig,
    PathKey,
    SubmissionState,
    load_path_config,
    path_ceiling_estimate,
    validate_path_config,
)
from course_supporter.llm.registry import load_registry
from course_supporter.models.source import AssignmentType
from course_supporter.service_logging import job_scope, record_funds_decision
from course_supporter.storage.orm import ExternalServiceCall, Job

pytestmark = pytest.mark.requires_db

_PATHS_FILE = Path("config/submission_paths.yaml")
_REGISTRY_FILE = Path("config/external_services.yaml")
_KEY = PathKey(AssignmentType.TASK, SubmissionState.FIRST)


@pytest.fixture()
async def committed_job_id(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> AsyncGenerator[uuid.UUID]:
    async with session_factory() as session:
        job = Job(
            tenant_id=committed_seeds["tenant_id"],
            course_node_id=committed_seeds["course_node_id"],
            job_type="document_processing",
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    yield job_id

    async with session_factory() as session:
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id == job_id)
        )
        await session.execute(delete(Job).where(Job.id == job_id))
        await session.commit()


def _context(tenant_id: uuid.UUID) -> SubmissionContext:
    return SubmissionContext(
        tenant_id=tenant_id,
        student_id=uuid.uuid4(),
        submission_id=uuid.uuid4(),
        path_key=_KEY,
    )


def _startup_config(path: Path) -> PathConfig:
    """Read and check a path file, as a process does at startup."""
    config = load_path_config(path)
    validate_path_config(config, load_registry(_REGISTRY_FILE))
    return config


def _shipped_file_with_raised_ceiling(tmp_path: Path) -> Path:
    """The shipped path file with one money ceiling on ``task/first`` raised by 0.03."""
    raw = yaml.safe_load(_PATHS_FILE.read_text(encoding="utf-8"))
    edited_stage = raw["task_types"]["task"]["paths"]["first"][0]
    raw["stages"][edited_stage]["ceilings"]["money_usd"] += 0.03
    path = tmp_path / "submission_paths.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _artificial_file(tmp_path: Path) -> Path:
    """A path no shipped file describes: two stages on ``task/first``."""

    def stage(money_usd: float) -> dict[str, Any]:
        return {
            "deterministic": False,
            # The router-facing fields (task 03). The prompt is a real one —
            # startup checks that every stage names a prompt it can read, and
            # this fixture goes through the same startup check.
            "prompt_ref": "prompts/safety_check/v1.md",
            "requires": [],
            "input_budget_ratio": None,
            "record_output": False,
            "ceilings": {
                "tool_steps": 0,
                "money_usd": money_usd,
                "output_tokens": 1024,
            },
            "ladder": [
                {
                    "provider": "gemini",
                    "model": "gemini-2.5-flash",
                    "reasoning": None,
                    "max_output_tokens": None,
                }
            ],
        }

    data = {
        "stages": {"first_stage": stage(0.03), "second_stage": stage(0.02)},
        "task_types": {
            "test": {
                "served_by": "todays_mentor",
                "paths": {state.value: [] for state in SubmissionState},
            },
            "short_task": {"served_by": "todays_mentor", "paths": {}},
            "task": {
                "served_by": "todays_mentor",
                "paths": {
                    "first": ["first_stage", "second_stage"],
                    "repeat_without_replies": ["first_stage"],
                    "repeat_with_replies": ["first_stage"],
                },
            },
            "project": {"served_by": "todays_mentor", "paths": {}},
        },
    }
    path = tmp_path / "artificial_paths.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


async def _register_rows(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> list[ExternalServiceCall]:
    async with session_factory() as session:
        result = await session.execute(
            select(ExternalServiceCall).where(ExternalServiceCall.job_id == job_id)
        )
        return list(result.scalars())


class _RefusingFundsPort:
    """Stands in for a billing adapter that finds the payer's balance short.

    It records its answer through the register writer the first implementation
    uses, so the test also sees where a refusal's reason lands.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.answer = FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)

    async def check_and_reserve(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        await record_funds_decision(
            self._session_factory,
            path_key=context.path_key,
            ceiling_estimate_usd=ceiling_estimate_usd,
            answer=self.answer,
        )
        return self.answer

    async def account_stage_cost(
        self, context: SubmissionContext, stage_cost_usd: float
    ) -> None:
        """A refused submission never reaches a stage."""

    async def release_remainder(
        self, context: SubmissionContext, outcome: SubmissionOutcome
    ) -> None:
        """Nothing was held."""

    async def check_and_reserve_for_version(
        self, context: VersionWorkContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        """A work-once-per-version is refused the same way a submission is."""
        await record_funds_decision(
            self._session_factory,
            path_key=None,
            ceiling_estimate_usd=ceiling_estimate_usd,
            answer=self.answer,
        )
        return self.answer

    async def account_version_work_cost(
        self, context: VersionWorkContext, actual_usd: float
    ) -> None:
        """Nothing was held."""


class TestCeilingEditReachesTheRecord:
    """Acceptance 3: editing a ceiling in the file changes the recorded estimate."""

    async def test_two_files_one_money_ceiling_apart_record_two_estimates(
        self,
        tmp_path: Path,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        committed_job_id: uuid.UUID,
    ) -> None:
        """Same code, two files: the shipped one and a copy with one stage's
        money ceiling raised."""
        estimates = [
            path_ceiling_estimate(_startup_config(path), _KEY)
            for path in (_PATHS_FILE, _shipped_file_with_raised_ceiling(tmp_path))
        ]
        port = AlwaysEnoughFundsPort(session_factory)
        with job_scope(committed_job_id):
            for estimate in estimates:
                await port.check_and_reserve(
                    _context(committed_seeds["tenant_id"]), estimate
                )

        rows = await _register_rows(session_factory, committed_job_id)
        assert Counter(row.ceiling_estimate_usd for row in rows) == Counter(estimates)
        assert estimates[1] - estimates[0] == pytest.approx(0.03)


class TestWorkThatIsNotASubmission:
    """Task 06 decision 1: a work-once-per-version speaks its own two operations."""

    async def test_the_row_carries_the_job_and_no_trace_of_a_submission(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        committed_job_id: uuid.UUID,
    ) -> None:
        """One row, the work's own job, and ``path_key`` empty rather than invented.

        The check that matters is the LAST one. A generation has no submission
        and no path; if the port filled ``path_key`` with something plausible
        to keep the column busy, every later reading of the register would
        count this work as a submission down that path.
        """
        context = VersionWorkContext(
            tenant_id=committed_seeds["tenant_id"],
            authored_document_id=uuid.uuid4(),
            source_content_hash="c" * 64,
            work_kind=VersionWorkKind.KEY_EXPLANATION,
        )
        port: FundsPort = AlwaysEnoughFundsPort(session_factory)

        with job_scope(committed_job_id):
            answer = await port.check_and_reserve_for_version(context, 0.03)
            await port.account_version_work_cost(context, 0.021)

        assert answer == FundsAnswer.allowed()
        # One row: nothing is held, so accounting the actual cost adds none.
        (row,) = await _register_rows(session_factory, committed_job_id)
        assert row.action == FUNDS_PORT_ACTION
        assert row.job_id == committed_job_id, "the row belongs to the work's job"
        assert row.ceiling_estimate_usd == pytest.approx(0.03)
        assert row.funds_decision == FundsDecision.ALLOWED
        assert row.funds_refusal_reason is None
        assert row.path_key is None, "a work-once-per-version takes no path"
        # A row without a model call (KD5), as for a submission.
        assert row.provider is None
        assert row.model_id is None
        assert row.success is None
        assert row.cost_usd is None
        assert row.error_message is None
        assert row.outcome is None

    async def test_a_refusing_implementation_records_its_reason_the_same_way(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        committed_job_id: uuid.UUID,
    ) -> None:
        """A replacement refuses a generation exactly as it refuses a submission."""
        implementation = _RefusingFundsPort(session_factory)
        port: FundsPort = implementation
        context = VersionWorkContext(
            tenant_id=committed_seeds["tenant_id"],
            authored_document_id=uuid.uuid4(),
            source_content_hash="d" * 64,
            work_kind=VersionWorkKind.KEY_EXPLANATION,
        )

        with job_scope(committed_job_id):
            answer = await port.check_and_reserve_for_version(context, 0.03)

        assert answer.decision is FundsDecision.REFUSED
        (row,) = await _register_rows(session_factory, committed_job_id)
        assert row.funds_decision == FundsDecision.REFUSED
        assert row.funds_refusal_reason == FundsRefusalReason.INSUFFICIENT_FUNDS
        assert row.error_message is None, "a refusal is an answer, not a failed call"
        assert row.path_key is None


class TestThreeOperationsOnAnArtificialPath:
    """Acceptance 4: the three operations, with the test as the caller."""

    async def test_always_enough_allows_records_the_numbers_and_holds_nothing(
        self,
        tmp_path: Path,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        committed_job_id: uuid.UUID,
    ) -> None:
        config = _startup_config(_artificial_file(tmp_path))
        estimate = path_ceiling_estimate(config, _KEY)
        context = _context(committed_seeds["tenant_id"])
        port: FundsPort = AlwaysEnoughFundsPort(session_factory)

        with job_scope(committed_job_id):
            answer = await port.check_and_reserve(context, estimate)
            await port.account_stage_cost(context, 0.004)
            await port.release_remainder(context, SubmissionOutcome.COMPLETED)

        assert answer == FundsAnswer.allowed()
        # One row: nothing was held, so the stage price and the outcome add none.
        (row,) = await _register_rows(session_factory, committed_job_id)
        assert row.action == FUNDS_PORT_ACTION
        assert row.ceiling_estimate_usd == estimate
        assert estimate == pytest.approx(0.05)
        assert row.funds_decision == FundsDecision.ALLOWED
        assert row.funds_refusal_reason is None
        assert row.path_key == "task/first"
        # A row without a model call (KD5).
        assert row.provider is None
        assert row.model_id is None
        assert row.success is None
        assert row.cost_usd is None
        assert row.error_message is None
        assert row.outcome is None

    async def test_a_refusal_reaches_the_caller_unchanged_and_is_not_a_failure(
        self,
        tmp_path: Path,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        committed_job_id: uuid.UUID,
    ) -> None:
        estimate = path_ceiling_estimate(
            _startup_config(_artificial_file(tmp_path)), _KEY
        )
        implementation = _RefusingFundsPort(session_factory)
        port: FundsPort = implementation

        with job_scope(committed_job_id):
            answer = await port.check_and_reserve(
                _context(committed_seeds["tenant_id"]), estimate
            )

        assert answer is implementation.answer
        assert answer.decision is FundsDecision.REFUSED
        assert answer.refusal_reason is FundsRefusalReason.INSUFFICIENT_FUNDS

        (row,) = await _register_rows(session_factory, committed_job_id)
        assert row.funds_decision == FundsDecision.REFUSED
        assert row.funds_refusal_reason == FundsRefusalReason.INSUFFICIENT_FUNDS
        assert row.ceiling_estimate_usd == estimate
        assert row.error_message is None

        async with session_factory() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE success IS NULL), "
                        "count(*) FILTER "
                        "(WHERE NOT success OR error_message IS NOT NULL) "
                        "FROM external_service_calls WHERE job_id = :job"
                    ),
                    {"job": committed_job_id},
                )
            ).one()
        # Premise first: the row really has a NULL success, then the count.
        assert tuple(counts) == (1, 0)
