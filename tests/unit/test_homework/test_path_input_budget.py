"""The input budget of a type's new path, from its stages (project path, step 2)."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from course_supporter.config import get_settings
from course_supporter.homework.path_config import PathConfig, load_path_config
from course_supporter.homework.text_budget import path_context_budget_chars
from course_supporter.llm.registry import ModelRegistryConfig, load_registry
from course_supporter.models.source import AssignmentType


def _rung(model: str) -> dict[str, Any]:
    return {
        "provider": "p",
        "model": model,
        "reasoning": None,
        "max_output_tokens": None,
    }


def _model_stage(model: str, ratio: float | None) -> dict[str, Any]:
    return {
        "deterministic": False,
        "ceilings": {"tool_steps": 0, "money_usd": 0.1, "output_tokens": 1000},
        "prompt_ref": "x/v1.md",
        "requires": [],
        "input_budget_ratio": ratio,
        "record_output": False,
        "ladder": [_rung(model), _rung("other")],
    }


def _config(stages: dict[str, Any], task_paths: list[str]) -> PathConfig:
    return PathConfig.model_validate(
        {
            "stages": stages,
            "task_types": {
                "task": {
                    "served_by": "new_path",
                    "paths": {
                        "first": task_paths,
                        "repeat_without_replies": task_paths,
                        "repeat_with_replies": [],
                    },
                }
            },
        }
    )


def _registry(**windows: int | None) -> ModelRegistryConfig:
    models = {name: SimpleNamespace(max_context=w) for name, w in windows.items()}
    return cast(ModelRegistryConfig, SimpleNamespace(models=models))


def test_the_tightest_first_rung_bounds_the_input() -> None:
    config = _config(
        {"a": _model_stage("big", 0.5), "b": _model_stage("small", 0.5)},
        ["a", "b"],
    )
    registry = _registry(big=1_000_000, small=100_000, other=10)
    assert path_context_budget_chars(AssignmentType.TASK, config, registry) == 100_000


def test_each_stage_applies_its_own_ratio() -> None:
    config = _config(
        {"a": _model_stage("m", 0.5), "b": _model_stage("m", 0.25)},
        ["a", "b"],
    )
    registry = _registry(m=100_000, other=10)
    assert path_context_budget_chars(AssignmentType.TASK, config, registry) == 50_000


def test_a_code_stage_and_a_null_ratio_do_not_bound() -> None:
    config = _config(
        {
            "code": {"code": True, "deterministic": True},
            "free": _model_stage("tiny", None),
            "a": _model_stage("m", 0.5),
        },
        ["code", "free", "a"],
    )
    registry = _registry(m=100_000, tiny=10, other=10)
    assert path_context_budget_chars(AssignmentType.TASK, config, registry) == 100_000


def test_a_type_without_bounding_stages_has_no_budget() -> None:
    config = _config({"code": {"code": True, "deterministic": True}}, ["code"])
    assert path_context_budget_chars(AssignmentType.TASK, config, _registry()) is None
    assert (
        path_context_budget_chars(AssignmentType.PROJECT, config, _registry()) is None
    )


def test_a_first_rung_without_a_window_is_refused() -> None:
    config = _config({"a": _model_stage("m", 0.5)}, ["a"])
    with pytest.raises(RuntimeError, match="declares no max_context"):
        path_context_budget_chars(AssignmentType.TASK, config, _registry(m=None))


def test_the_checked_in_task_path_has_a_budget() -> None:
    settings = get_settings()
    config = load_path_config(settings.submission_paths_config_path)
    registry = load_registry(settings.external_services_path)
    budget = path_context_budget_chars(AssignmentType.TASK, config, registry)
    assert budget is not None
    assert budget > 0
