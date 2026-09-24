"""The checked-in submission-path file (config/submission_paths.yaml)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from course_supporter.config import get_settings
from course_supporter.homework.path_config import (
    PathConfig,
    PathKey,
    ServedBy,
    SubmissionState,
    load_path_config,
    path_ceiling_estimate,
    validate_path_config,
)
from course_supporter.homework.path_stages import validate_stage_executors
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.registry import ModelRegistryConfig, load_registry
from course_supporter.models.source import AssignmentType

_PATHS_FILE = Path("config/submission_paths.yaml")
_REGISTRY_FILE = Path("config/external_services.yaml")

# Starting content of both stage definitions: (provider, model, pin). A change
# here is a conscious recalibration of the new path, not of today's Mentor.
_COPIED_RUNGS = [
    ("mistral", "mistral-small-latest", None),
    ("deepseek", "deepseek-v4-flash", 8192),
    ("gemini", "gemini-2.5-flash", None),
]


@pytest.fixture(scope="module")
def config() -> PathConfig:
    return load_path_config(_PATHS_FILE)


@pytest.fixture(scope="module")
def registry() -> ModelRegistryConfig:
    return load_registry(_REGISTRY_FILE)


class TestCheckedInPaths:
    def test_file_passes_the_startup_checks(
        self, config: PathConfig, registry: ModelRegistryConfig
    ) -> None:
        # Exactly what the app and the worker run at boot: the shipped ladders
        # supply the names a path stage may not reuse, and the prompt files are
        # resolved from the repository root, as in production. Since task 07
        # this is also the proof that the switch of ``test`` passes the boot.
        validate_path_config(
            config,
            registry,
            ladder_stage_names=load_ladder_config(
                get_settings().ladders_dir
            ).stages.keys(),
        )
        validate_stage_executors(config.stages)

    def test_no_stage_name_repeats_a_ladder_stage(self, config: PathConfig) -> None:
        """Shared names would make the register sum two stages' costs as one."""
        ladder_names = set(load_ladder_config(get_settings().ladders_dir).stages)

        assert set(config.stages) & ladder_names == set()

    def test_every_stage_names_a_prompt_that_exists(self, config: PathConfig) -> None:
        for name, stage in config.stages.items():
            assert Path(stage.prompt_ref).is_file(), (
                f"stage {name!r} names a prompt that is not there: {stage.prompt_ref}"
            )

    def test_only_test_is_switched(self, config: PathConfig) -> None:
        """Task 07 ships with ``test`` on the new path and the other three not.

        Until task 07 every type stood on today's Mentor. A type moves by an
        edit of this one assertion, beside the file's own line — never as a
        side effect of some other change.
        """
        assert {t: d.served_by for t, d in config.task_types.items()} == {
            AssignmentType.TEST: ServedBy.NEW_PATH,
            AssignmentType.SHORT_TASK: ServedBy.TODAYS_MENTOR,
            AssignmentType.TASK: ServedBy.TODAYS_MENTOR,
            AssignmentType.PROJECT: ServedBy.TODAYS_MENTOR,
        }

    def test_nine_paths_are_described_and_short_task_has_none(
        self, config: PathConfig
    ) -> None:
        described = [
            PathKey(t, s) for t, d in config.task_types.items() for s in d.paths
        ]
        assert len(described) == 9
        assert config.task_types[AssignmentType.SHORT_TASK].paths == {}

    def test_test_paths_make_no_model_call(self, config: PathConfig) -> None:
        paths = config.task_types[AssignmentType.TEST].paths
        assert paths == {state: [] for state in SubmissionState}

    @pytest.mark.parametrize("task_type", [AssignmentType.TASK, AssignmentType.PROJECT])
    def test_classifier_on_first_submission_only(
        self, config: PathConfig, task_type: AssignmentType
    ) -> None:
        paths = config.task_types[task_type].paths
        assert paths[SubmissionState.FIRST] == ["safety", "attempt_classifier"]
        assert paths[SubmissionState.REPEAT_WITHOUT_REPLIES] == ["safety"]
        assert paths[SubmissionState.REPEAT_WITH_REPLIES] == ["safety"]

    @pytest.mark.parametrize(
        ("task_type", "state", "estimate"),
        [
            (AssignmentType.TEST, SubmissionState.FIRST, 0.0),
            (AssignmentType.TASK, SubmissionState.FIRST, 0.10),
            (AssignmentType.TASK, SubmissionState.REPEAT_WITH_REPLIES, 0.05),
            (AssignmentType.PROJECT, SubmissionState.FIRST, 0.10),
            (AssignmentType.PROJECT, SubmissionState.REPEAT_WITHOUT_REPLIES, 0.05),
        ],
    )
    def test_path_ceiling_estimates(
        self,
        config: PathConfig,
        task_type: AssignmentType,
        state: SubmissionState,
        estimate: float,
    ) -> None:
        key = PathKey(task_type, state)
        assert path_ceiling_estimate(config, key) == pytest.approx(estimate)

    @pytest.mark.parametrize("stage_name", ["safety", "attempt_classifier"])
    def test_stage_starts_from_the_copied_rungs(
        self, config: PathConfig, stage_name: str
    ) -> None:
        stage = config.stages[stage_name]
        rungs = [(r.provider, r.model, r.max_output_tokens) for r in stage.ladder]

        assert rungs == _COPIED_RUNGS
        assert all(r.reasoning is None for r in stage.ladder)
        assert stage.deterministic is True
        assert (
            stage.ceilings.tool_steps,
            stage.ceilings.money_usd,
            stage.ceilings.output_tokens,
        ) == (0, 0.05, 8192)


class TestShortTaskPathsByConfigurationAlone:
    """Acceptance 5: a type's paths are added by editing the file, not the code."""

    def test_copy_with_short_task_paths_passes_the_startup_checks(
        self, tmp_path: Path, registry: ModelRegistryConfig
    ) -> None:
        raw = yaml.safe_load(_PATHS_FILE.read_text(encoding="utf-8"))
        raw["task_types"]["short_task"]["paths"] = {
            "first": ["safety", "attempt_classifier"],
            "repeat_without_replies": ["safety"],
            "repeat_with_replies": ["safety"],
        }
        copy_path = tmp_path / "submission_paths.yaml"
        copy_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

        edited = load_path_config(copy_path)
        validate_path_config(edited, registry)

        described = sum(len(d.paths) for d in edited.task_types.values())
        assert described == 12
        key = PathKey(AssignmentType.SHORT_TASK, SubmissionState.FIRST)
        assert path_ceiling_estimate(edited, key) == pytest.approx(0.10)
