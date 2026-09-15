"""The checked-in submission-path file (config/submission_paths.yaml)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from course_supporter.homework.path_config import (
    PathConfig,
    PathKey,
    ServedBy,
    SubmissionState,
    load_path_config,
    path_ceiling_estimate,
    validate_path_config,
)
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
        validate_path_config(config, registry)

    def test_every_type_is_served_by_todays_mentor(self, config: PathConfig) -> None:
        assert {t: d.served_by for t, d in config.task_types.items()} == {
            t: ServedBy.TODAYS_MENTOR for t in AssignmentType
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
