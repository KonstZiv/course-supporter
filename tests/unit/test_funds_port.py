"""The funds port interface (mentor-rebuild task 02, acceptance 4 and 6).

Nothing in this task calls the port, so the caller is the test itself; the
refusing implementation below stands in for a billing adapter.
"""

from __future__ import annotations

import dataclasses
import inspect
import uuid
from unittest.mock import MagicMock

import pytest

from course_supporter.call_outcome import FundsDecision
from course_supporter.funds_port import (
    AlwaysEnoughFundsPort,
    FundsAnswer,
    FundsPort,
    FundsRefusalReason,
    SubmissionContext,
    SubmissionOutcome,
)
from course_supporter.homework.path_config import PathKey, SubmissionState
from course_supporter.models.source import AssignmentType

_KEY = PathKey(AssignmentType.TASK, SubmissionState.FIRST)


def _context() -> SubmissionContext:
    return SubmissionContext(
        tenant_id=uuid.uuid4(),
        student_id=uuid.uuid4(),
        submission_id=uuid.uuid4(),
        path_key=_KEY,
    )


class _RefusingFundsPort:
    """A replacement implementation that refuses, satisfying the port structurally."""

    def __init__(self) -> None:
        self.answer = FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)
        self.heard: list[tuple[str, SubmissionContext, object]] = []

    async def check_and_reserve(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        self.heard.append(("check_and_reserve", context, ceiling_estimate_usd))
        return self.answer

    async def account_stage_cost(
        self, context: SubmissionContext, stage_cost_usd: float
    ) -> None:
        self.heard.append(("account_stage_cost", context, stage_cost_usd))

    async def release_remainder(
        self, context: SubmissionContext, outcome: SubmissionOutcome
    ) -> None:
        self.heard.append(("release_remainder", context, outcome))


class TestFundsAnswer:
    def test_an_allowance_carries_no_reason(self) -> None:
        answer = FundsAnswer.allowed()

        assert answer.decision is FundsDecision.ALLOWED
        assert answer.refusal_reason is None

    def test_a_refusal_carries_its_reason(self) -> None:
        answer = FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)

        assert answer.decision is FundsDecision.REFUSED
        assert answer.refusal_reason is FundsRefusalReason.INSUFFICIENT_FUNDS

    @pytest.mark.parametrize(
        ("decision", "reason"),
        [
            pytest.param(FundsDecision.REFUSED, None, id="refusal-without-reason"),
            pytest.param(
                FundsDecision.ALLOWED,
                FundsRefusalReason.INSUFFICIENT_FUNDS,
                id="allowance-with-reason",
            ),
        ],
    )
    def test_an_answer_breaking_the_reason_rule_cannot_be_built(
        self, decision: FundsDecision, reason: FundsRefusalReason | None
    ) -> None:
        with pytest.raises(ValueError, match="carries a refusal reason exactly when"):
            FundsAnswer(decision=decision, refusal_reason=reason)

    def test_an_answer_cannot_be_edited_on_its_way_to_the_caller(self) -> None:
        answer = FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)

        with pytest.raises(dataclasses.FrozenInstanceError):
            answer.refusal_reason = None  # type: ignore[misc]


class TestSubmissionContext:
    def test_context_is_immutable(self) -> None:
        context = _context()

        with pytest.raises(dataclasses.FrozenInstanceError):
            context.student_id = uuid.uuid4()  # type: ignore[misc]

    def test_context_is_built_by_keyword_only(self) -> None:
        """Three fields are UUIDs: a positional call could swap them silently."""
        ids = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

        with pytest.raises(TypeError):
            SubmissionContext(*ids, _KEY)  # type: ignore[misc]


class TestReplaceableImplementation:
    async def test_a_refusal_reaches_the_caller_unchanged(self) -> None:
        implementation = _RefusingFundsPort()
        port: FundsPort = implementation

        answer = await port.check_and_reserve(_context(), 0.1)

        assert answer is implementation.answer
        assert answer.decision is FundsDecision.REFUSED
        assert answer.refusal_reason is FundsRefusalReason.INSUFFICIENT_FUNDS

    async def test_every_operation_hears_the_same_context_and_its_own_input(
        self,
    ) -> None:
        implementation = _RefusingFundsPort()
        port: FundsPort = implementation
        context = _context()

        await port.check_and_reserve(context, 0.1)
        await port.account_stage_cost(context, 0.004)
        await port.release_remainder(context, SubmissionOutcome.FAILED)

        assert implementation.heard == [
            ("check_and_reserve", context, 0.1),
            ("account_stage_cost", context, 0.004),
            ("release_remainder", context, SubmissionOutcome.FAILED),
        ]

    def test_the_port_is_told_nothing_about_stage_provider_or_model(self) -> None:
        """Invariant 3: each operation takes the context and one input, no more."""
        parameters = {
            name: list(inspect.signature(getattr(FundsPort, name)).parameters)
            for name in ("check_and_reserve", "account_stage_cost", "release_remainder")
        }

        assert parameters == {
            "check_and_reserve": ["self", "context", "ceiling_estimate_usd"],
            "account_stage_cost": ["self", "context", "stage_cost_usd"],
            "release_remainder": ["self", "context", "outcome"],
        }


class TestAlwaysEnoughFundsPort:
    async def test_the_answer_does_not_wait_on_the_register(self) -> None:
        """Outside a job context the register skips the row; the caller still
        hears "allowed" — the record is a side effect, never the answer."""
        from course_supporter.service_logging import _current_job_id

        session_factory = MagicMock()
        token = _current_job_id.set(None)
        try:
            answer = await AlwaysEnoughFundsPort(session_factory).check_and_reserve(
                _context(), 0.1
            )
        finally:
            _current_job_id.reset(token)

        # Premise: no write was attempted.
        session_factory.assert_not_called()
        assert answer == FundsAnswer.allowed()
