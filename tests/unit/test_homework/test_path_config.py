"""Submission-path configuration: shape, startup checks, ceiling estimate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from course_supporter.homework.path_config import (
    PathKey,
    ServedBy,
    SubmissionState,
    load_path_config,
    path_ceiling_estimate,
    validate_path_config,
)
from course_supporter.llm.registry import ModelRegistryConfig
from course_supporter.models.source import AssignmentType

_PROMPT_NAME = "prompt.md"


def _rung(**overrides: Any) -> dict[str, Any]:
    rung: dict[str, Any] = {
        "provider": "anthropic",
        "model": "priced",
        "reasoning": None,
        "max_output_tokens": None,
    }
    rung.update(overrides)
    return rung


def _stage(money: float = 0.05, **overrides: Any) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "deterministic": True,
        "prompt_ref": _PROMPT_NAME,
        "requires": [],
        "input_budget_ratio": None,
        "record_output": False,
        "ceilings": {"tool_steps": 0, "money_usd": money, "output_tokens": 8192},
        "ladder": [_rung(), _rung(max_output_tokens=4096)],
    }
    stage.update(overrides)
    return stage


def _config() -> dict[str, Any]:
    """A valid file: two stages, a test type with empty paths, a type without."""
    return {
        "stages": {"safety": _stage(0.05), "classifier": _stage(0.02)},
        "task_types": {
            "test": {
                "served_by": "todays_mentor",
                "paths": {state.value: [] for state in SubmissionState},
            },
            "short_task": {"served_by": "todays_mentor", "paths": {}},
            "task": {
                "served_by": "todays_mentor",
                "paths": {
                    "first": ["safety", "classifier"],
                    "repeat_without_replies": ["safety"],
                    "repeat_with_replies": ["safety"],
                },
            },
            "project": {"served_by": "todays_mentor", "paths": {}},
        },
    }


def _registry() -> ModelRegistryConfig:
    return ModelRegistryConfig.model_validate(
        {
            "providers": {
                "anthropic": {
                    "type": "llm",
                    "models": [
                        {"id": "priced", "cost_per_1k_in": 0.0, "cost_per_1k_out": 0.0},
                        {"id": "unpriced"},
                    ],
                }
            },
            "actions": {},
        }
    )


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    # Every stage's prompt must exist and parse for validation to pass, so the
    # fixture writes one beside the config and the checks resolve against
    # ``tmp_path`` instead of the repository's real prompt tree.
    (tmp_path / _PROMPT_NAME).write_text("## System\nbody\n", encoding="utf-8")
    path = tmp_path / "submission_paths.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _load_error(tmp_path: Path, data: dict[str, Any]) -> str:
    with pytest.raises(ValueError, match="Invalid submission path config") as exc_info:
        load_path_config(_write(tmp_path, data))
    return str(exc_info.value)


def _validation_error(tmp_path: Path, data: dict[str, Any]) -> str:
    config = load_path_config(_write(tmp_path, data))
    with pytest.raises(ValueError, match="Submission path validation failed") as exc:
        validate_path_config(config, _registry(), prompt_base_path=tmp_path)
    return str(exc.value)


class TestShape:
    def test_valid_file_loads_and_validates(self, tmp_path: Path) -> None:
        config = load_path_config(_write(tmp_path, _config()))
        validate_path_config(config, _registry(), prompt_base_path=tmp_path)

        assert (
            config.task_types[AssignmentType.TASK].served_by is ServedBy.TODAYS_MENTOR
        )
        assert config.task_types[AssignmentType.SHORT_TASK].paths == {}
        assert config.stages["safety"].ladder[0].max_output_tokens is None

    def test_stage_without_ladder_is_a_hole(self, tmp_path: Path) -> None:
        data = _config()
        del data["stages"]["safety"]["ladder"]
        msg = _load_error(tmp_path, data)
        assert "stages.safety.ladder" in msg
        assert "Field required" in msg

    def test_stage_with_empty_ladder_is_a_hole(self, tmp_path: Path) -> None:
        data = _config()
        data["stages"]["safety"]["ladder"] = []
        assert "stages.safety.ladder" in _load_error(tmp_path, data)

    @pytest.mark.parametrize("ceiling", ["tool_steps", "money_usd", "output_tokens"])
    def test_stage_without_a_ceiling_is_a_hole(
        self, tmp_path: Path, ceiling: str
    ) -> None:
        data = _config()
        del data["stages"]["safety"]["ceilings"][ceiling]
        msg = _load_error(tmp_path, data)
        assert f"stages.safety.ceilings.{ceiling}" in msg
        assert "Field required" in msg

    def test_stage_without_deterministic_flag_is_a_hole(self, tmp_path: Path) -> None:
        data = _config()
        del data["stages"]["safety"]["deterministic"]
        msg = _load_error(tmp_path, data)
        assert "stages.safety.deterministic" in msg

    @pytest.mark.parametrize("key", ["reasoning", "max_output_tokens"])
    def test_rung_key_has_no_default(self, tmp_path: Path, key: str) -> None:
        data = _config()
        del data["stages"]["safety"]["ladder"][0][key]
        assert f"stages.safety.ladder.0.{key}" in _load_error(tmp_path, data)

    def test_type_outside_code_enum_fails(self, tmp_path: Path) -> None:
        data = _config()
        data["task_types"]["quiz"] = {"served_by": "todays_mentor", "paths": {}}
        assert "task_types.quiz" in _load_error(tmp_path, data)

    def test_state_outside_the_three_fails(self, tmp_path: Path) -> None:
        data = _config()
        data["task_types"]["task"]["paths"]["second"] = ["safety"]
        assert "task_types.task.paths.second" in _load_error(tmp_path, data)

    @pytest.mark.parametrize(
        ("ceiling", "value"),
        [("money_usd", 0), ("tool_steps", -1), ("output_tokens", 0)],
    )
    def test_ceiling_out_of_range_fails(
        self, tmp_path: Path, ceiling: str, value: int
    ) -> None:
        data = _config()
        data["stages"]["safety"]["ceilings"][ceiling] = value
        assert f"stages.safety.ceilings.{ceiling}" in _load_error(tmp_path, data)

    def test_unknown_key_fails(self, tmp_path: Path) -> None:
        data = _config()
        data["stages"]["safety"]["temperature"] = 0
        assert "stages.safety.temperature" in _load_error(tmp_path, data)

    def test_shape_faults_are_reported_together(self, tmp_path: Path) -> None:
        data = _config()
        del data["stages"]["safety"]["ladder"]
        del data["stages"]["classifier"]["deterministic"]
        msg = _load_error(tmp_path, data)
        assert "stages.safety.ladder" in msg
        assert "stages.classifier.deterministic" in msg

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_path_config(tmp_path / "absent.yaml")


class TestValidation:
    def test_pin_above_stage_output_ceiling_fails(self, tmp_path: Path) -> None:
        data = _config()
        data["stages"]["safety"]["ladder"][1]["max_output_tokens"] = 8193
        msg = _validation_error(tmp_path, data)
        assert (
            "Stage 'safety' rung 1 pins max_output_tokens=8193 above the stage "
            "output ceiling 8192"
        ) in msg

    def test_pin_equal_to_stage_output_ceiling_passes(self, tmp_path: Path) -> None:
        data = _config()
        data["stages"]["safety"]["ladder"][1]["max_output_tokens"] = 8192
        validate_path_config(
            load_path_config(_write(tmp_path, data)),
            _registry(),
            prompt_base_path=tmp_path,
        )

    @pytest.mark.parametrize(
        ("rung", "expected"),
        [
            (
                {"model": "typo"},
                "Stage 'safety' rung 0 references unknown model: 'typo'",
            ),
            (
                {"model": "unpriced"},
                "Stage 'safety' rung 0 model 'unpriced' has no named price",
            ),
            (
                {"provider": "gemini", "reasoning": {"exclude": True}},
                "Stage 'safety' rung 0 provider 'gemini' model 'priced' declares "
                "a reasoning form its connector cannot translate",
            ),
        ],
        ids=["unknown-model", "unpriced", "untranslatable-reasoning"],
    )
    def test_rung_rejected_by_the_shared_rule_fails(
        self, tmp_path: Path, rung: dict[str, Any], expected: str
    ) -> None:
        data = _config()
        data["stages"]["safety"]["ladder"][0].update(rung)
        assert expected in _validation_error(tmp_path, data)

    def test_every_code_type_must_be_declared(self, tmp_path: Path) -> None:
        data = _config()
        del data["task_types"]["project"]
        assert "Task type 'project' is not declared" in _validation_error(
            tmp_path, data
        )

    def test_type_with_some_states_fails(self, tmp_path: Path) -> None:
        data = _config()
        del data["task_types"]["task"]["paths"]["repeat_with_replies"]
        msg = _validation_error(tmp_path, data)
        assert "Task type 'task' describes some submission states" in msg
        assert "repeat_with_replies" in msg

    def test_new_path_type_without_paths_fails(self, tmp_path: Path) -> None:
        data = _config()
        data["task_types"]["project"]["served_by"] = "new_path"
        assert (
            "Task type 'project' is switched to the new path but describes no paths"
        ) in _validation_error(tmp_path, data)

    def test_new_path_type_with_all_paths_passes(self, tmp_path: Path) -> None:
        data = _config()
        data["task_types"]["task"]["served_by"] = "new_path"
        validate_path_config(
            load_path_config(_write(tmp_path, data)),
            _registry(),
            prompt_base_path=tmp_path,
        )

    def test_described_path_is_checked_whatever_the_switch(
        self, tmp_path: Path
    ) -> None:
        data = _config()
        data["task_types"]["task"]["paths"]["first"] = ["safety", "verdicts"]
        assert "Path 'task/first' lists undefined stage 'verdicts'" in (
            _validation_error(tmp_path, data)
        )

    def test_test_path_with_a_stage_fails(self, tmp_path: Path) -> None:
        data = _config()
        data["task_types"]["test"]["paths"]["repeat_with_replies"] = ["safety"]
        assert (
            "Path 'test/repeat_with_replies' lists stages ['safety'], but a test "
            "path makes no model call"
        ) in _validation_error(tmp_path, data)

    def test_test_paths_without_stages_pass(self, tmp_path: Path) -> None:
        config = load_path_config(_write(tmp_path, _config()))
        test_paths = config.task_types[AssignmentType.TEST].paths

        assert test_paths and all(stages == [] for stages in test_paths.values())
        validate_path_config(config, _registry(), prompt_base_path=tmp_path)

    def test_stage_listed_twice_in_a_path_fails(self, tmp_path: Path) -> None:
        data = _config()
        data["task_types"]["task"]["paths"]["first"] = ["safety", "safety"]
        assert "Path 'task/first' lists stage 'safety' more than once" in (
            _validation_error(tmp_path, data)
        )

    def test_validation_faults_are_reported_together(self, tmp_path: Path) -> None:
        data = _config()
        data["stages"]["safety"]["ladder"][0]["model"] = "typo"
        data["stages"]["classifier"]["ladder"][1]["max_output_tokens"] = 9000
        data["task_types"]["task"]["paths"]["first"] = ["safety", "verdicts"]
        del data["task_types"]["project"]
        msg = _validation_error(tmp_path, data)
        assert "references unknown model: 'typo'" in msg
        assert "Stage 'classifier' rung 1 pins max_output_tokens=9000" in msg
        assert "lists undefined stage 'verdicts'" in msg
        assert "Task type 'project' is not declared" in msg


class TestRouterFacingFields:
    """What the router needs to execute a path stage (task 03)."""

    def test_missing_prompt_file_is_a_validation_fault(self, tmp_path: Path) -> None:
        data = _config()
        data["stages"]["safety"]["prompt_ref"] = "prompts/gone/v1.md"

        assert "Stage 'safety' prompt cannot be read" in _validation_error(
            tmp_path, data
        )

    def test_unparseable_prompt_file_is_a_validation_fault(
        self, tmp_path: Path
    ) -> None:
        """Existing is not enough — a file the loader cannot read is a hole too."""
        data = _config()
        data["stages"]["safety"]["prompt_ref"] = "broken.md"
        config = load_path_config(_write(tmp_path, data))
        (tmp_path / "broken.md").write_text("no role header at all", encoding="utf-8")

        with pytest.raises(ValueError, match="Submission path validation failed") as e:
            validate_path_config(config, _registry(), prompt_base_path=tmp_path)

        assert "Stage 'safety' prompt cannot be read" in str(e.value)

    def test_stage_name_may_not_repeat_a_ladder_stage(self, tmp_path: Path) -> None:
        """Two stages with one name would have their register costs summed."""
        config = load_path_config(_write(tmp_path, _config()))

        with pytest.raises(ValueError, match="Submission path validation failed") as e:
            validate_path_config(
                config,
                _registry(),
                ladder_stage_names={"safety", "criteria_decomposition"},
                prompt_base_path=tmp_path,
            )

        msg = str(e.value)
        assert "Stage 'safety' reuses the name of a stage in the model ladders" in msg
        # Only the colliding one is reported.
        assert "Stage 'classifier' reuses" not in msg

    def test_a_capability_the_model_lacks_is_a_validation_fault(
        self, tmp_path: Path
    ) -> None:
        data = _config()
        data["stages"]["safety"]["requires"] = ["vision"]

        msg = _validation_error(tmp_path, data)

        # The message is the ladder validator's, word for word (it renders the
        # capability as the enum member's repr; noisy, but identical on both
        # sides, which is the point). Assert the part that carries meaning.
        assert "Stage 'safety' rung 0 model 'priced' lacks required" in msg
        assert "vision" in msg

    def test_input_budget_ratio_without_a_context_window_is_a_fault(
        self, tmp_path: Path
    ) -> None:
        data = _config()
        data["stages"]["safety"]["input_budget_ratio"] = 0.5

        assert (
            "Stage 'safety' rung 0 model 'priced' has no max_context in the "
            "registry (required by input_budget_ratio=0.5)"
        ) in _validation_error(tmp_path, data)

    def test_ratio_out_of_range_is_a_shape_fault(self, tmp_path: Path) -> None:
        data = _config()
        data["stages"]["safety"]["input_budget_ratio"] = 1.5

        assert "stages.safety.input_budget_ratio" in _load_error(tmp_path, data)


class TestCeilingEstimate:
    def test_estimate_is_the_sum_of_money_ceilings(self, tmp_path: Path) -> None:
        config = load_path_config(_write(tmp_path, _config()))
        first = PathKey(AssignmentType.TASK, SubmissionState.FIRST)
        repeat = PathKey(AssignmentType.TASK, SubmissionState.REPEAT_WITHOUT_REPLIES)

        assert path_ceiling_estimate(config, first) == pytest.approx(0.07)
        assert path_ceiling_estimate(config, repeat) == pytest.approx(0.05)

    def test_path_without_model_stages_estimates_zero(self, tmp_path: Path) -> None:
        config = load_path_config(_write(tmp_path, _config()))
        key = PathKey(AssignmentType.TEST, SubmissionState.FIRST)
        assert path_ceiling_estimate(config, key) == 0.0

    def test_output_and_tool_ceilings_do_not_enter_the_sum(
        self, tmp_path: Path
    ) -> None:
        data = _config()
        data["stages"]["safety"]["ceilings"].update(tool_steps=5, output_tokens=65536)
        data["stages"]["safety"]["ladder"][1]["max_output_tokens"] = 65536
        config = load_path_config(_write(tmp_path, data))
        key = PathKey(AssignmentType.TASK, SubmissionState.REPEAT_WITH_REPLIES)
        assert path_ceiling_estimate(config, key) == pytest.approx(0.05)

    def test_changing_a_ceiling_in_the_file_changes_the_estimate(
        self, tmp_path: Path
    ) -> None:
        key = PathKey(AssignmentType.TASK, SubmissionState.FIRST)
        before = path_ceiling_estimate(
            load_path_config(_write(tmp_path, _config())), key
        )
        edited = _config()
        edited["stages"]["classifier"]["ceilings"]["money_usd"] = 0.5
        after = path_ceiling_estimate(load_path_config(_write(tmp_path, edited)), key)

        assert before == pytest.approx(0.07)
        assert after == pytest.approx(0.55)

    def test_undescribed_path_has_no_estimate(self, tmp_path: Path) -> None:
        config = load_path_config(_write(tmp_path, _config()))
        key = PathKey(AssignmentType.SHORT_TASK, SubmissionState.FIRST)
        with pytest.raises(KeyError, match="short_task/first"):
            path_ceiling_estimate(config, key)
