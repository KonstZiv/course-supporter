"""The version of the evaluator of a task type (task 13, lock a).

One version per type: every model stage on any path of the type, by the
stage's name. The same configuration gives the same value; another prompt of
any model stage of any path, another first rung, other code rules give
another; a later rung, a code stage and the path a stage is listed on change
nothing — so ``first`` and ``repeat_*`` share one version by construction. No
network, no database: the prompts live in ``tmp_path``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import structlog

from course_supporter.homework import criteria_verdicts
from course_supporter.homework.evaluator_version import evaluator_version
from course_supporter.homework.path_config import PathConfig, load_path_config
from course_supporter.llm import prompt_loader_md
from course_supporter.models.source import AssignmentType

_REPO_ROOT = Path(__file__).resolve().parents[3]

_TASK = AssignmentType.TASK

# Every model stage on a path of ``task`` below: ``classifier`` on ``first``
# alone, ``recheck`` on the ``repeat_*`` paths alone, the rest on all three.
_MODEL_STAGES = ("safety", "classifier", "evaluation", "explanation", "recheck")
# A model stage on a path of ``short_task`` alone.
_ANOTHER_TYPE_S = "short_only"


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
    """A ``task`` whose paths differ, a code stage between its model stages."""
    repeat = ["safety", "evaluation", "links", "explanation", "recheck"]
    task_paths = {
        "first": ["safety", "classifier", "evaluation", "links", "explanation"],
        "repeat_without_replies": list(repeat),
        "repeat_with_replies": list(repeat),
    }
    short_paths = {
        state: ["safety", _ANOTHER_TYPE_S, "explanation"] for state in task_paths
    }
    return {
        "stages": {
            **{name: _model_stage(name) for name in (*_MODEL_STAGES, _ANOTHER_TYPE_S)},
            "links": {"code": True, "deterministic": True},
        },
        "task_types": {
            "task": {"served_by": "new_path", "test_mode": True, "paths": task_paths},
            "short_task": {"served_by": "new_path", "paths": short_paths},
            "project": {"served_by": "todays_mentor", "paths": task_paths},
            "test": {
                "served_by": "new_path",
                "paths": {state: [] for state in task_paths},
            },
        },
    }


@pytest.fixture()
def prompts(tmp_path: Path) -> Path:
    """One prompt file per model stage, under ``tmp_path``; returns the base."""
    (tmp_path / "prompts").mkdir()
    for name in (*_MODEL_STAGES, _ANOTHER_TYPE_S):
        _write_prompt(tmp_path, name, f"You judge {name}.")
    return tmp_path


def _write_prompt(base: Path, name: str, system: str) -> None:
    (base / "prompts" / f"{name}.md").write_text(
        f"## System\n{system}\n\n## User\n{{{{ submission }}}}\n", encoding="utf-8"
    )


def _version(
    raw: dict[str, Any], base: Path, *, task_type: AssignmentType = _TASK
) -> str | None:
    return evaluator_version(
        task_type, PathConfig.model_validate(raw), prompt_base_path=base
    )


class TestLockAEvaluatorVersion:
    """Lock a. What changes the version, and what does not."""

    def test_the_same_configuration_gives_the_same_value(self, prompts: Path) -> None:
        value = _version(_raw(), prompts)

        assert value is not None
        assert re.fullmatch(r"ev1:[0-9a-f]{16}", value)
        assert _version(_raw(), prompts) == value

    @pytest.mark.parametrize("stage", _MODEL_STAGES)
    def test_another_prompt_of_any_model_stage_of_any_path_is_another_version(
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

    def test_other_code_rules_are_another_version(
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
        for stages in without["task_types"]["task"]["paths"].values():
            stages.remove("links")
        not_deterministic = _raw()
        not_deterministic["stages"]["links"]["deterministic"] = False
        two_code_stages = _raw()
        two_code_stages["stages"]["more_links"] = {"code": True, "deterministic": True}
        two_code_stages["task_types"]["task"]["paths"]["first"].insert(0, "more_links")

        assert _version(without, prompts) == before
        assert _version(not_deterministic, prompts) == before
        assert _version(two_code_stages, prompts) == before

    def test_first_and_repeat_share_one_version_by_construction(
        self, prompts: Path
    ) -> None:
        """Where a stage is listed, and in what order, is not in it.

        The attempt classifier moved from ``first`` to the ``repeat_*`` paths,
        a path's order reversed: the same version. ``first`` asks it and the
        ``repeat_*`` paths do not — one version still: it is the type's.
        """
        before = _version(_raw(), prompts)

        moved = _raw()
        paths = moved["task_types"]["task"]["paths"]
        paths["first"].remove("classifier")
        paths["repeat_with_replies"].append("classifier")
        reordered = _raw()
        reordered["task_types"]["task"]["paths"]["first"].reverse()

        assert _version(moved, prompts) == before
        assert _version(reordered, prompts) == before

    def test_a_model_stage_gone_from_every_path_is_another_version(
        self, prompts: Path
    ) -> None:
        before = _version(_raw(), prompts)
        raw = _raw()
        raw["task_types"]["task"]["paths"]["first"].remove("classifier")

        assert _version(raw, prompts) != before

    def test_a_stage_of_another_type_alone_is_not_in_it(self, prompts: Path) -> None:
        task_before = _version(_raw(), prompts)
        short_before = _version(_raw(), prompts, task_type=AssignmentType.SHORT_TASK)

        _write_prompt(prompts, _ANOTHER_TYPE_S, "You judge differently now.")

        assert _version(_raw(), prompts) == task_before
        assert (
            _version(_raw(), prompts, task_type=AssignmentType.SHORT_TASK)
            != short_before
        )


class TestNoVersion:
    """``None``: not on the new path, or no model stage on any path."""

    def test_a_type_served_by_todays_mentor(self, prompts: Path) -> None:
        assert _version(_raw(), prompts, task_type=AssignmentType.PROJECT) is None

    def test_a_type_with_no_model_stage(self, prompts: Path) -> None:
        assert _version(_raw(), prompts, task_type=AssignmentType.TEST) is None

        code_only = _raw()
        for state in code_only["task_types"]["task"]["paths"]:
            code_only["task_types"]["task"]["paths"][state] = ["links"]
        assert _version(code_only, prompts) is None

    def test_a_type_the_file_does_not_declare(self, prompts: Path) -> None:
        raw = _raw()
        del raw["task_types"]["short_task"]

        assert _version(raw, prompts, task_type=AssignmentType.SHORT_TASK) is None

    def test_a_type_with_no_paths_described(self, prompts: Path) -> None:
        raw = _raw()
        raw["task_types"]["task"]["paths"] = {}

        assert _version(raw, prompts) is None


class TestShippedFile:
    """The shipped file: the text types have a version, a test and a project not."""

    def test_versions(self) -> None:
        shipped = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")

        def version(task_type: AssignmentType) -> str | None:
            return evaluator_version(task_type, shipped, prompt_base_path=_REPO_ROOT)

        task = version(AssignmentType.TASK)
        assert task is not None
        # The two text types walk the same stages: one evaluator.
        assert version(AssignmentType.SHORT_TASK) == task
        assert version(AssignmentType.TEST) is None
        assert version(AssignmentType.PROJECT) is None
