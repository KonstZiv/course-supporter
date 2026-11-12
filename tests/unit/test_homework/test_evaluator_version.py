"""The version of the evaluator that writes a review (task 13, lock a).

A pure function over the submission-path file and the prompt files: the same
configuration gives the same value; another prompt of any model stage on the
path, another first rung, other verdict rules give another; a later rung and a
code stage change nothing. No network, no database: the prompts live in
``tmp_path``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import structlog

from course_supporter.homework import criteria_verdicts
from course_supporter.homework.evaluator_version import (
    evaluator_version,
    evaluator_versions_in_force,
)
from course_supporter.homework.path_config import (
    PathConfig,
    SubmissionState,
    load_path_config,
)
from course_supporter.llm import prompt_loader_md
from course_supporter.models.source import AssignmentType

_REPO_ROOT = Path(__file__).resolve().parents[3]

_FIRST = SubmissionState.FIRST
_REPEAT = SubmissionState.REPEAT_WITHOUT_REPLIES
_TASK = AssignmentType.TASK

# Every model stage of the ``first`` path of ``task`` below, in its order.
_MODEL_STAGES = ("safety", "classifier", "evaluation", "explanation")


def _rung(provider: str, model: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "reasoning": None,
        "max_output_tokens": None,
    }


def _model_stage(name: str) -> dict[str, Any]:
    return {
        "deterministic": True,
        "prompt_ref": f"prompts/{name}.md",
        "requires": [],
        "input_budget_ratio": None,
        "record_output": False,
        "ceilings": {"tool_steps": 0, "money_usd": 0.05, "output_tokens": 8192},
        "ladder": [_rung("mistral", f"{name}-first"), _rung("deepseek", "second")],
    }


def _raw() -> dict[str, Any]:
    """A ``task`` with a code stage between its model stages, as shipped."""
    paths = {
        "first": ["safety", "classifier", "evaluation", "links", "explanation"],
        "repeat_without_replies": ["safety", "evaluation", "links", "explanation"],
        "repeat_with_replies": ["safety", "evaluation", "links", "explanation"],
    }
    return {
        "stages": {
            **{name: _model_stage(name) for name in _MODEL_STAGES},
            "links": {"code": True, "deterministic": True},
        },
        "task_types": {
            "task": {"served_by": "new_path", "test_mode": True, "paths": paths},
            "short_task": {"served_by": "new_path", "paths": paths},
            "project": {"served_by": "todays_mentor", "paths": paths},
            "test": {
                "served_by": "new_path",
                "paths": {state: [] for state in paths},
            },
        },
    }


@pytest.fixture()
def prompts(tmp_path: Path) -> Path:
    """One prompt file per model stage, under ``tmp_path``; returns the base."""
    (tmp_path / "prompts").mkdir()
    for name in _MODEL_STAGES:
        _write_prompt(tmp_path, name, f"You judge {name}.")
    return tmp_path


def _write_prompt(base: Path, name: str, system: str) -> None:
    (base / "prompts" / f"{name}.md").write_text(
        f"## System\n{system}\n\n## User\n{{{{ submission }}}}\n", encoding="utf-8"
    )


def _version(
    raw: dict[str, Any],
    base: Path,
    *,
    task_type: AssignmentType = _TASK,
    state: SubmissionState = _FIRST,
) -> str | None:
    return evaluator_version(
        task_type, state, PathConfig.model_validate(raw), prompt_base_path=base
    )


class TestLockAEvaluatorVersion:
    """Lock a. What changes the version, and what does not."""

    def test_the_same_configuration_gives_the_same_value(self, prompts: Path) -> None:
        value = _version(_raw(), prompts)

        assert value is not None
        assert re.fullmatch(r"ev1:[0-9a-f]{16}", value)
        assert _version(_raw(), prompts) == value

    @pytest.mark.parametrize("stage", _MODEL_STAGES)
    def test_another_prompt_of_any_model_stage_is_another_version(
        self, prompts: Path, stage: str
    ) -> None:
        before = _version(_raw(), prompts)

        _write_prompt(prompts, stage, f"You judge {stage}, and more strictly.")

        assert _version(_raw(), prompts) != before

    def test_an_editorial_section_the_model_never_reads_is_not(
        self, prompts: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hash is the loader's: of the sections the model is sent.

        The loader warns about the section it drops. Its module logger is
        swapped for a fresh one: bound here (``cache_logger_on_first_use``),
        it would keep the processors of this test and slip past the
        ``capture_logs`` of the loader's own tests.
        """
        monkeypatch.setattr(prompt_loader_md, "logger", structlog.get_logger())
        before = _version(_raw(), prompts)
        path = prompts / "prompts" / "evaluation.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n## Examples\nNot sent.\n",
            encoding="utf-8",
        )

        assert _version(_raw(), prompts) == before

    @pytest.mark.parametrize("stage", _MODEL_STAGES)
    @pytest.mark.parametrize(
        ("field", "value"), [("model", "another-model"), ("provider", "gemini")]
    )
    def test_another_first_rung_is_another_version(
        self, prompts: Path, stage: str, field: str, value: str
    ) -> None:
        before = _version(_raw(), prompts)
        raw = _raw()
        raw["stages"][stage]["ladder"][0][field] = value

        assert _version(raw, prompts) != before

    def test_other_verdict_rules_are_another_version(
        self, prompts: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _version(_raw(), prompts)

        monkeypatch.setattr(
            criteria_verdicts,
            "VERDICT_RULES_VERSION",
            criteria_verdicts.VERDICT_RULES_VERSION + 1,
        )

        assert _version(_raw(), prompts) != before

    @pytest.mark.parametrize(
        "second_rung",
        [_rung("gemini", "another-second"), None],
        ids=["changed", "removed"],
    )
    def test_the_second_rung_is_not_in_it(
        self, prompts: Path, second_rung: dict[str, Any] | None
    ) -> None:
        before = _version(_raw(), prompts)
        raw = _raw()
        for name in _MODEL_STAGES:
            ladder = raw["stages"][name]["ladder"]
            raw["stages"][name]["ladder"] = (
                [ladder[0], second_rung] if second_rung is not None else [ladder[0]]
            )

        assert _version(raw, prompts) == before

    def test_a_code_stage_is_not_in_it(self, prompts: Path) -> None:
        before = _version(_raw(), prompts)

        without = _raw()
        without["task_types"]["task"]["paths"]["first"].remove("links")
        not_deterministic = _raw()
        not_deterministic["stages"]["links"]["deterministic"] = False
        two_code_stages = _raw()
        two_code_stages["stages"]["more_links"] = {"code": True, "deterministic": True}
        two_code_stages["task_types"]["task"]["paths"]["first"].insert(0, "more_links")

        assert _version(without, prompts) == before
        assert _version(not_deterministic, prompts) == before
        assert _version(two_code_stages, prompts) == before

    def test_the_order_of_the_model_stages_is_in_it(self, prompts: Path) -> None:
        before = _version(_raw(), prompts)
        raw = _raw()
        first = raw["task_types"]["task"]["paths"]["first"]
        first[2], first[4] = first[4], first[2]

        assert _version(raw, prompts) != before

    def test_another_path_is_another_version(self, prompts: Path) -> None:
        """``first`` asks the attempt classifier; ``repeat_*`` does not."""
        assert _version(_raw(), prompts) != _version(_raw(), prompts, state=_REPEAT)


class TestNoVersion:
    """``None``: not on the new path, or no model stage on the path."""

    def test_a_type_served_by_todays_mentor(self, prompts: Path) -> None:
        assert _version(_raw(), prompts, task_type=AssignmentType.PROJECT) is None

    def test_a_path_with_no_model_stage(self, prompts: Path) -> None:
        assert _version(_raw(), prompts, task_type=AssignmentType.TEST) is None

        code_only = _raw()
        code_only["task_types"]["task"]["paths"]["first"] = ["links"]
        assert _version(code_only, prompts) is None

    def test_a_type_the_file_does_not_declare(self, prompts: Path) -> None:
        raw = _raw()
        del raw["task_types"]["short_task"]

        assert _version(raw, prompts, task_type=AssignmentType.SHORT_TASK) is None

    def test_a_type_with_no_paths_described(self, prompts: Path) -> None:
        raw = _raw()
        raw["task_types"]["project"]["paths"] = {}

        assert _version(raw, prompts, task_type=AssignmentType.PROJECT) is None


class TestVersionsInForce:
    """The versions a source of a copy may carry: every path of the type."""

    def _in_force(
        self,
        raw: dict[str, Any],
        base: Path,
        *,
        task_type: AssignmentType = _TASK,
        state: SubmissionState = _REPEAT,
    ) -> frozenset[str]:
        return evaluator_versions_in_force(
            task_type, state, PathConfig.model_validate(raw), prompt_base_path=base
        )

    def test_a_repeat_may_be_given_a_review_written_on_first(
        self, prompts: Path
    ) -> None:
        in_force = self._in_force(_raw(), prompts)

        assert in_force == {
            _version(_raw(), prompts, state=_FIRST),
            _version(_raw(), prompts, state=_REPEAT),
        }

    def test_a_change_of_the_evaluator_leaves_the_old_version_out(
        self, prompts: Path
    ) -> None:
        old = _version(_raw(), prompts, state=_FIRST)

        _write_prompt(prompts, "classifier", "You judge differently now.")

        in_force = self._in_force(_raw(), prompts)
        assert old not in in_force
        assert _version(_raw(), prompts, state=_FIRST) in in_force

    def test_none_for_the_own_path_is_no_version_at_all(self, prompts: Path) -> None:
        raw = _raw()
        raw["task_types"]["task"]["paths"]["repeat_without_replies"] = ["links"]

        assert self._in_force(raw, prompts) == frozenset()
        assert self._in_force(_raw(), prompts, task_type=AssignmentType.TEST) == (
            frozenset()
        )
        assert (
            self._in_force(_raw(), prompts, task_type=AssignmentType.PROJECT)
            == frozenset()
        )


class TestShippedFile:
    """The shipped file: the text types have a version, a test and a project not."""

    @pytest.fixture()
    def shipped(self) -> PathConfig:
        return load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")

    def test_versions(self, shipped: PathConfig) -> None:
        def version(task_type: AssignmentType, state: SubmissionState) -> str | None:
            return evaluator_version(
                task_type, state, shipped, prompt_base_path=_REPO_ROOT
            )

        for task_type in (AssignmentType.TASK, AssignmentType.SHORT_TASK):
            first, repeat = version(task_type, _FIRST), version(task_type, _REPEAT)
            assert first is not None and repeat is not None
            assert first != repeat
            assert version(task_type, SubmissionState.REPEAT_WITH_REPLIES) == repeat
        # The two text types walk the same stages: one evaluator.
        assert version(AssignmentType.TASK, _FIRST) == version(
            AssignmentType.SHORT_TASK, _FIRST
        )
        for state in SubmissionState:
            assert version(AssignmentType.TEST, state) is None
            assert version(AssignmentType.PROJECT, state) is None
