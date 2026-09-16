"""Stage executors: resolution by name, and what the two of them reuse (task 03).

The point of the registry is that the body walks names and nothing else, and the
point of the two executors is that they add a reaction to today's own functions
rather than a second copy of them. Both are pinned here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from course_supporter.homework.path_config import (
    PathKey,
    PathStage,
    SubmissionState,
)
from course_supporter.homework.path_stages import (
    ATTEMPT_CLASSIFIER,
    SAFETY,
    StageContext,
    StageOutcome,
    get_stage_executor,
    missing_stage_executors,
    register_stage_executor,
    validate_stage_executors,
)
from course_supporter.llm.stage_router import StageExecution
from course_supporter.models.source import AssignmentType

if TYPE_CHECKING:
    from collections.abc import Iterator

_KEY = PathKey(AssignmentType.TASK, SubmissionState.FIRST)


def _stage() -> PathStage:
    return PathStage.model_validate(
        {
            "deterministic": True,
            "prompt_ref": "prompts/safety_check/v1.md",
            "requires": ["structured_output"],
            "input_budget_ratio": 0.5,
            "record_output": False,
            "ceilings": {"tool_steps": 0, "money_usd": 0.05, "output_tokens": 8192},
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
                    "reasoning": {"effort": "low"},
                    "max_output_tokens": 4096,
                },
            ],
        }
    )


def _context(stage_name: str = SAFETY) -> StageContext:
    return StageContext(
        session=AsyncMock(),
        router=AsyncMock(),
        submission=AsyncMock(),
        submission_text="text",
        language="uk",
        path_key=_KEY,
        stage_name=stage_name,
        stage=_stage(),
    )


@pytest.fixture()
def _restore_registry() -> Iterator[None]:
    """Put back whatever the module registered, whatever a test did to it."""
    saved = {
        SAFETY: get_stage_executor(SAFETY),
        ATTEMPT_CLASSIFIER: get_stage_executor(ATTEMPT_CLASSIFIER),
    }
    yield
    for name, executor in saved.items():
        register_stage_executor(name, executor)


class TestRegistry:
    def test_the_two_shipped_stages_resolve(self) -> None:
        assert get_stage_executor(SAFETY) is not None
        assert get_stage_executor(ATTEMPT_CLASSIFIER) is not None

    def test_an_unregistered_name_does_not_resolve(self) -> None:
        with pytest.raises(KeyError):
            get_stage_executor("verdicts")

    def test_missing_names_are_reported_together_and_sorted(self) -> None:
        assert missing_stage_executors([SAFETY, "zebra", "apple"]) == [
            "apple",
            "zebra",
        ]

    def test_a_described_stage_without_an_executor_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'verdicts'"):
            validate_stage_executors([SAFETY, "verdicts"])

    def test_the_shipped_stages_pass_the_check(self) -> None:
        validate_stage_executors([SAFETY, ATTEMPT_CLASSIFIER])

    async def test_an_executor_can_be_replaced_under_its_name(
        self, _restore_registry: None
    ) -> None:
        """Interface requirement: swap the part, touch no call site."""

        async def _other(_context: StageContext) -> StageOutcome:
            return StageOutcome.ends_path("mismatch", "swapped")

        register_stage_executor(SAFETY, _other)

        outcome = await get_stage_executor(SAFETY)(_context())

        assert outcome.reason_code == "swapped"


class TestWhatIsHandedToTodaysFunction:
    """The additive argument carries the path stage, field for field."""

    async def test_safety_runs_todays_check_on_the_paths_own_stage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        async def _fake_check(text: str, **kwargs: Any) -> Any:
            seen["text"] = text
            seen.update(kwargs)
            verdict = AsyncMock()
            verdict.is_safe = True
            verdict.model_dump = lambda **_: {"source": "stage2"}
            return verdict

        monkeypatch.setattr(
            "course_supporter.security.stage2.run_stage2_safety_check", _fake_check
        )
        monkeypatch.setattr(
            "course_supporter.storage.homework_repository.HomeworkRepository",
            lambda _session: AsyncMock(),
        )

        outcome = await get_stage_executor(SAFETY)(_context())

        assert outcome.carry_on is True
        execution = seen["execution"]
        assert isinstance(execution, StageExecution)
        assert execution.stage_name == SAFETY
        # The two limits that make it a path stage rather than a ladder stage.
        assert execution.stop_on_output_ceiling is True
        assert execution.money_ceiling_usd == 0.05
        # …and the router-facing fields, carried across unchanged.
        assert execution.stage.prompt_ref == "prompts/safety_check/v1.md"
        assert execution.stage.input_budget_ratio == 0.5
        assert execution.stage.record_output is False
        assert [(e.provider, e.model) for e in execution.stage.ladder] == [
            ("mistral", "m"),
            ("gemini", "g"),
        ]
        assert execution.stage.ladder[1].reasoning == {"effort": "low"}
        assert execution.stage.ladder[1].max_output_tokens == 4096

    async def test_an_unsafe_verdict_ends_the_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_check(_text: str, **_kwargs: Any) -> Any:
            verdict = AsyncMock()
            verdict.is_safe = False
            verdict.violations = []
            verdict.model_dump = lambda **_: {"source": "stage2"}
            return verdict

        monkeypatch.setattr(
            "course_supporter.security.stage2.run_stage2_safety_check", _fake_check
        )
        monkeypatch.setattr(
            "course_supporter.storage.homework_repository.HomeworkRepository",
            lambda _session: AsyncMock(),
        )

        outcome = await get_stage_executor(SAFETY)(_context())

        assert outcome.carry_on is False
        assert outcome.terminal_status == "rejected"
        assert outcome.reason_code == "stage2_rejected"


class TestTodaysCallersAreUnchanged:
    """The lock on the additive argument: absent, nothing moves.

    Both functions grew one optional parameter so the new path could reuse them.
    Every caller that existed before passes nothing, and must therefore still go
    through the by-name entry — the one that reads ``ladders_*.yaml``, descends
    on an empty answer and knows no money ceiling.
    """

    async def test_safety_check_without_execution_goes_by_name(self) -> None:
        from course_supporter.llm.stage_router import StageResult
        from course_supporter.security.stage2 import run_stage2_safety_check

        router = AsyncMock()
        router.execute_for_stage = AsyncMock(
            return_value=StageResult(
                content=(
                    '{"is_safe": true, "violations": [], '
                    '"reasoning": "ok", "confidence": 0.9}'
                ),
                provider_used="p",
                model_used="m",
                attempt_count=1,
            )
        )

        await run_stage2_safety_check("text", router=router)

        router.execute_for_stage.assert_awaited_once()
        assert router.execute_for_stage.await_args.args[0] == "safety_check"
        router.execute_stage.assert_not_awaited()

    async def test_the_authored_caller_still_gets_its_own_stage(self) -> None:
        """The other production caller of this function, unmoved."""
        from course_supporter.llm.stage_router import StageResult
        from course_supporter.security.stage2 import run_stage2_safety_check

        router = AsyncMock()
        router.execute_for_stage = AsyncMock(
            return_value=StageResult(
                content=(
                    '{"is_safe": true, "violations": [], '
                    '"reasoning": "ok", "confidence": 0.9}'
                ),
                provider_used="p",
                model_used="m",
                attempt_count=1,
            )
        )

        await run_stage2_safety_check("text", router=router, content_kind="authored")

        assert router.execute_for_stage.await_args.args[0] == "safety_check_authored"
        router.execute_stage.assert_not_awaited()

    async def test_sanity_agent_without_execution_goes_by_name(self) -> None:
        from course_supporter.agents.sanity import STAGE_SANITY, SanityAgent
        from course_supporter.models.sanity import SanityClassification

        router = AsyncMock()

        async def _execute(_name: str, **kwargs: Any) -> None:
            kwargs["response_validator"](
                SanityClassification(
                    verdict="match", confidence=0.9, reason="looks like one"
                ).model_dump_json()
            )

        router.execute_for_stage = AsyncMock(side_effect=_execute)

        await SanityAgent(router).classify(
            task_title="t",
            task_description="d",
            task_text="x",
            submission_text="s",
            language="Ukrainian",
        )

        router.execute_for_stage.assert_awaited_once()
        assert router.execute_for_stage.await_args.args[0] == STAGE_SANITY
        router.execute_stage.assert_not_awaited()


class TestTheBranchCostsNothingOnTodaysMentor:
    """What the homework body pays for this task when nothing is switched.

    In production after task 03 every type is on today's Mentor, and the branch
    must then be one scan of a dict already in memory: no session, no query, no
    file. A test that only checked the ANSWER would pass even if the branch
    opened a session first — so this one checks that it never asked for one.
    """

    async def test_no_session_is_opened_when_no_type_is_switched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from course_supporter.homework import path_runner
        from course_supporter.homework.path_config import PathConfig

        config = PathConfig.model_validate(
            {
                "stages": {},
                "task_types": {
                    t.value: {
                        "served_by": "todays_mentor",
                        "paths": {},
                    }
                    for t in AssignmentType
                },
            }
        )
        monkeypatch.setattr(path_runner, "get_path_config", lambda: config)
        opened = 0

        def _factory() -> object:
            nonlocal opened
            opened += 1
            raise AssertionError("the branch opened a session")

        answered = await path_runner.run_new_path_if_switched(
            {"session_factory": _factory}, uuid4(), uuid4()
        )

        assert answered is False
        assert opened == 0
